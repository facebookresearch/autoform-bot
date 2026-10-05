"""Report what revising an article's Lean declarations would affect.

``autoform work impact`` answers the question a worker must settle before
changing declarations other work may build on: which articles now state
something else, whose proofs may stop elaborating, which helpers no article
names sit in between, and whether the Markdown roadmap records those
dependencies. The answer is read from the elaborated environment by a Lean
probe rather than from source text, so uses that only automation such as
``simp`` introduces are seen too.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import skeleton
from .lean import SourceIndex, index_project
from .runtime import load_runtime_graph
from .skeleton import LeanLibrary, SkeletonError, _lean_name, _lean_name_parts, lean_libraries, path_of
from .work import work_context

IMPACT_SCHEMA = "autoform-impact/v1"

#: Every line the impact probe wants read back starts with this marker.
IMPACT_MARKER = "AUTOFORM_IMPACT "

_KINDS = frozenset({"axiom", "constructor", "def", "inductive", "opaque", "quot", "recursor", "theorem"})
#: Kinds whose value belongs to their meaning, as in the skeleton probe's
#: ``meaningConstants``; an inductive's constructors stand in for its value.
_VALUE_IS_MEANING = frozenset({"def", "inductive", "opaque"})
#: Kinds with a body that elaborates against the statements it uses.
_HAS_BODY = frozenset({"def", "opaque", "theorem"})


class ImpactError(ValueError):
    """The question cannot be asked: nothing to revise, or a name that is not local."""


# --------------------------------------------------------------------------- #
# Probe records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ConstantRecord:
    """One project-local constant as the impact probe read it.

    ``type_uses`` and ``value_uses`` name only project-local constants.
    ``internal`` is ``Name.isInternalDetail`` of the user-facing name, so a
    private declaration someone wrote is not internal, while the companions
    Lean generates (``_proof_1``, ``match_1``, ``_simp_1``) are.
    ``user_name`` is the user-facing name of a private constant, which is how
    articles and ``--declaration`` name it, and ``None`` for any other.
    ``alias_of`` is the local constant a theorem's value is exactly when its
    type is exactly that constant's too, as Batteries' ``alias`` writes.
    """

    name: str
    kind: str
    module: str
    type_uses: tuple[str, ...] = ()
    value_uses: tuple[str, ...] = ()
    instance: bool = False
    internal: bool = False
    parent: str | None = None
    deprecated: bool = False
    replacement: str | None = None
    uses_deprecated: tuple[str, ...] = ()
    value_missing: bool = False
    user_name: str | None = None
    alias_of: str | None = None

    @property
    def meaning_uses(self) -> tuple[str, ...]:
        """The local constants this constant's meaning rests on.

        An alias's statement is its target's, so it changes with the target's.
        """

        uses = self.type_uses + self.value_uses if self.kind in _VALUE_IS_MEANING else self.type_uses
        return (*uses, self.alias_of) if self.alias_of is not None else uses


_RECORD_FIELDS: dict[str, tuple[type, ...]] = {
    "alias_of": (str, type(None)),
    "deprecated": (bool,),
    "instance": (bool,),
    "internal": (bool,),
    "kind": (str,),
    "module": (str,),
    "name": (str,),
    "parent": (str, type(None)),
    "replacement": (str, type(None)),
    "type_uses": (list,),
    "user_name": (str, type(None)),
    "uses_deprecated": (list,),
    "value_missing": (bool,),
    "value_uses": (list,),
}


def parse_impact_output(text: str) -> dict[str, ConstantRecord]:
    """Return the probe's strictly validated records, keyed by constant name.

    A record whose value the probe could not read fails closed: the users of
    whatever that value mentions could not be traced.
    """

    records: dict[str, ConstantRecord] = {}
    for line in text.splitlines():
        if not line.startswith(IMPACT_MARKER):
            continue
        try:
            payload = json.loads(line[len(IMPACT_MARKER) :])
        except json.JSONDecodeError as exc:
            raise SkeletonError([f"the impact probe emitted invalid JSON: {exc}"]) from exc
        record = _constant_record(payload)
        if record.name in records:
            raise SkeletonError([f"the impact probe emitted {record.name} twice"])
        records[record.name] = record
    if not records:
        raise SkeletonError(
            ["the impact probe found no project-local constants; are the library roots and globs built?"]
        )
    for record in records.values():
        if record.value_missing:
            raise SkeletonError([f"the impact probe could not read the value of {record.name}"])
        unknown = sorted(set(record.type_uses + record.value_uses) - records.keys())
        if unknown:
            raise SkeletonError([f"the impact probe reported {record.name} using unknown constants: {unknown}"])
    return records


def _constant_record(payload: object) -> ConstantRecord:
    if not isinstance(payload, dict) or set(payload) != set(_RECORD_FIELDS):
        raise SkeletonError(["the impact probe emitted a record with unexpected fields"])
    for field, expected in _RECORD_FIELDS.items():
        value = payload[field]
        if not isinstance(value, expected) or (
            isinstance(value, list) and not all(isinstance(item, str) for item in value)
        ):
            raise SkeletonError([f"the impact probe emitted a malformed {field!r} field"])
    if (
        not payload["name"]
        or not payload["module"]
        or payload["kind"] not in _KINDS
        or payload["user_name"] == ""
        or payload["alias_of"] not in (None, *payload["value_uses"])
    ):
        raise SkeletonError([f"the impact probe emitted a malformed record for {payload['name']!r}"])
    return ConstantRecord(
        name=payload["name"],
        kind=payload["kind"],
        module=payload["module"],
        type_uses=tuple(payload["type_uses"]),
        value_uses=tuple(payload["value_uses"]),
        instance=payload["instance"],
        internal=payload["internal"],
        parent=payload["parent"],
        deprecated=payload["deprecated"],
        replacement=payload["replacement"],
        uses_deprecated=tuple(payload["uses_deprecated"]),
        value_missing=payload["value_missing"],
        user_name=payload["user_name"],
        alias_of=payload["alias_of"],
    )


def _name_key(name: str) -> tuple[object, ...]:
    """Compare names by component, so ``A.«b»`` and ``A.b`` are the same name."""

    try:
        parts = _lean_name_parts(name)
    except SkeletonError:
        return (name,)
    return tuple((part, not quoted and part.isascii() and part.isdigit()) for part, quoted in parts)


# --------------------------------------------------------------------------- #
# Project modules and the probe
# --------------------------------------------------------------------------- #

#: Module name components the probe can import without quoting.
_MODULE_COMPONENT = re.compile(r"[A-Za-z_][A-Za-z0-9_'!?]*")


def project_modules(libraries: Sequence[LeanLibrary]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the modules Lake builds for ``libraries`` and the local module prefixes.

    A library's ``globs`` select its modules as Lake's ``Glob`` does: ``M`` is
    one module, ``M.*`` the module and its submodules, ``M.+`` its strict
    submodules, found by walking the source tree like ``forEachModuleInDir``.
    A library without globs builds its roots. A module is local when it equals
    or lies under a library root or a glob base.
    """

    modules: set[str] = set()
    prefixes: set[str] = set()
    for library in libraries:
        prefixes.update(library.roots)
        for glob in library.globs or library.roots:
            base, mode = _glob(glob, library)
            prefixes.add(base)
            directory = library.src_dir.joinpath(*base.split("."))
            if mode != "+":
                if not directory.with_suffix(".lean").is_file():
                    raise SkeletonError([f"lean_lib {library.name}: module {base} has no source file"])
                modules.add(base)
            if mode:
                if not directory.is_dir():
                    raise SkeletonError(
                        [f"lean_lib {library.name}: glob {glob} names no source directory {base.replace('.', '/')}"]
                    )
                modules.update(f"{base}.{module}" for module in _submodules(directory, library))
    if not modules:
        raise SkeletonError(["the Lake configuration selects no modules"])
    return tuple(sorted(modules)), tuple(sorted(prefixes))


def _glob(glob: str, library: LeanLibrary) -> tuple[str, str]:
    base, mode = glob, ""
    if glob.endswith((".*", ".+")):
        base, mode = glob[:-2], glob[-1]
    if not base or not all(_MODULE_COMPONENT.fullmatch(part) for part in base.split(".")):
        raise SkeletonError(
            [
                f"lean_lib {library.name}: cannot read module glob {glob!r}; the impact probe supports "
                "`M`, `M.*` and `M.+` over plain module names"
            ]
        )
    return base, mode


def _submodules(directory: Path, library: LeanLibrary) -> list[str]:
    """Every ``.lean`` file below ``directory`` as a relative module name."""

    found: list[str] = []
    visited: set[Path] = set()

    def walk(path: Path, prefix: tuple[str, ...]) -> None:
        resolved = path.resolve()
        if resolved in visited:
            return
        visited.add(resolved)
        with os.scandir(path) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                if entry.is_dir():
                    walk(Path(entry.path), (*prefix, entry.name))
                elif Path(entry.name).suffix == ".lean":
                    parts = (*prefix, Path(entry.name).stem)
                    if not all(_MODULE_COMPONENT.fullmatch(part) for part in parts):
                        raise SkeletonError(
                            [
                                f"lean_lib {library.name}: cannot import {entry.path}; the impact probe supports "
                                "plain module names only"
                            ]
                        )
                    found.append(".".join(parts))

    walk(directory, ())
    return found


def _impact_template() -> str:
    """The Lean impact probe source, kept under probes/ beside this module."""

    return (Path(__file__).parent / "probes" / "impact_probe.lean").read_text(encoding="utf-8")


def render_impact_probe(*, imports: Iterable[str], project_roots: Iterable[str]) -> str:
    """Render the Lean program that records every project-local constant."""

    modules = sorted(set(imports))
    if not modules:
        raise SkeletonError(["refusing to render an impact probe with no imports"])
    return _impact_template().format(
        imports="\n".join(f"import {module}" for module in modules),
        marker=IMPACT_MARKER,
        output_env=skeleton.PROBE_OUTPUT_ENV,
        output_limit=skeleton.DEFAULT_PROBE_OUTPUT_LIMIT,
        project_roots=", ".join(_lean_name(name) for name in sorted(set(project_roots))),
    )


# --------------------------------------------------------------------------- #
# The impact computation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ImpactArticle:
    """An article as the impact computation sees it: its Lean names and Markdown dependencies."""

    id: str
    article_id: str | None
    declarations: tuple[str, ...]
    dependencies: tuple[str, ...] = ()

    @property
    def claim_target(self) -> str:
        return self.article_id or self.id


@dataclass(frozen=True, slots=True)
class ImpactedArticle:
    """An article other than the revised one, with its impacted declarations."""

    id: str
    article_id: str | None
    claim_target: str
    declarations: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "article_id": self.article_id,
            "claim_target": self.claim_target,
            "declarations": list(self.declarations),
        }


@dataclass(frozen=True, slots=True)
class ImpactHelper:
    """An impacted constant that no article names and that is not an internal detail."""

    name: str
    kind: str
    impact: str
    module: str
    path: str | None
    line: int | None
    owner: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "impact": self.impact,
            "module": self.module,
            "path": self.path,
            "line": self.line,
            "owner": self.owner,
        }


@dataclass(frozen=True, slots=True)
class DeprecatedConstant:
    """A local deprecated constant, its replacement, and what still refers to it.

    ``users`` are the local constants that use it; ``articles`` are the
    articles other than the revised one whose ``lean:`` names it. The revised
    article does not count, since it drops the name in the commit that deletes
    the constant.
    """

    name: str
    replacement: str | None
    users: tuple[str, ...]
    articles: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "replacement": self.replacement,
            "users": list(self.users),
            "articles": list(self.articles),
        }


@dataclass(frozen=True, slots=True)
class ImpactReport:
    """What revising ``declarations`` of ``article`` affects."""

    source_revision: str
    article: ImpactArticle
    declarations: tuple[str, ...]
    statement_impacted: tuple[ImpactedArticle, ...]
    proof_impacted: tuple[ImpactedArticle, ...]
    helpers: tuple[ImpactHelper, ...]
    undeclared_dependencies: tuple[str, ...]
    deprecated: tuple[DeprecatedConstant, ...]
    deprecated_unused: tuple[str, ...]
    claim_targets: tuple[str, ...]

    @property
    def contained(self) -> bool:
        """Whether nothing outside the revised article is impacted.

        A helper the revised article owns, such as a structure's generated
        constructor or recursor, is repaired under that article's claim, so it
        does not count; any other helper, owned or not, does.
        """

        return not (
            self.statement_impacted
            or self.proof_impacted
            or any(helper.owner != self.article.id for helper in self.helpers)
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": IMPACT_SCHEMA,
            "source_revision": self.source_revision,
            "article": {
                "id": self.article.id,
                "article_id": self.article.article_id,
                "claim_target": self.article.claim_target,
            },
            "declarations": list(self.declarations),
            "contained": self.contained,
            "statement_impacted": [item.as_dict() for item in self.statement_impacted],
            "proof_impacted": [item.as_dict() for item in self.proof_impacted],
            "helpers": [helper.as_dict() for helper in self.helpers],
            "undeclared_dependencies": list(self.undeclared_dependencies),
            "deprecated": [item.as_dict() for item in self.deprecated],
            "deprecated_unused": list(self.deprecated_unused),
            "claim_targets": list(self.claim_targets),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


Locator = Callable[[ConstantRecord], tuple[str | None, int | None]]


def compute_impact(
    records: Mapping[str, ConstantRecord],
    articles: Sequence[ImpactArticle],
    revised: ImpactArticle,
    declarations: Sequence[str],
    *,
    source_revision: str,
    locate: Locator | None = None,
) -> ImpactReport:
    """Compute what revising ``declarations`` of article ``revised`` affects.

    The statement-impacted set M is the reverse closure of the revised names
    over meaning edges: a constant's type, plus the value of a definition or
    opaque constant and the constructors of an inductive. The proof-impacted
    set P holds the constants with a body, outside M, whose value uses M
    directly or through internal-detail constants, such as the ``_simp_1``
    companion ``simp`` uses or the ``_proof_1`` a definition's nested proof
    becomes. Definitions belong in P too: their nested proofs can stop
    elaborating although their meaning is unchanged. A theorem whose value and
    type are exactly another constant and its type, as Batteries' ``alias``
    writes, has that constant's statement, so it joins M with it.
    """

    resolve = _resolver(records)
    revised_names: dict[str, str] = {}
    missing: list[str] = []
    for name in declarations:
        record_name = resolve(name)
        if record_name is None:
            missing.append(name)
        else:
            revised_names.setdefault(record_name, name)
    if missing:
        raise ImpactError("not a project-local constant: " + ", ".join(dict.fromkeys(missing)))
    if not revised_names:
        raise ImpactError(f"{revised.id}: nothing to revise; pass --declaration NAME")

    meaning_users: dict[str, set[str]] = {}
    users: dict[str, set[str]] = {}
    for record in records.values():
        for used in record.meaning_uses:
            meaning_users.setdefault(used, set()).add(record.name)
        for used in (*record.type_uses, *record.value_uses):
            users.setdefault(used, set()).add(record.name)

    meaning = _reverse_closure(revised_names, meaning_users)
    through_internal = _reverse_closure(meaning, users, admit=lambda name: records[name].internal)
    proof = {
        record.name
        for record in records.values()
        if record.kind in _HAS_BODY
        and record.name not in meaning
        and any(used in through_internal for used in record.value_uses)
    }

    named: dict[str, list[str]] = {}
    statement_impacted: list[ImpactedArticle] = []
    proof_impacted: list[ImpactedArticle] = []
    for article in articles:
        resolved = [(name, resolve(name, f"{article.id}: lean: ")) for name in article.declarations]
        for _, record_name in resolved:
            if record_name is not None:
                named.setdefault(record_name, []).append(article.id)
        if article.id == revised.id:
            continue
        in_meaning = tuple(name for name, record_name in resolved if record_name in meaning)
        in_proof = tuple(name for name, record_name in resolved if record_name in proof)
        if in_meaning:
            statement_impacted.append(
                ImpactedArticle(article.id, article.article_id, article.claim_target, in_meaning)
            )
        elif in_proof:
            proof_impacted.append(ImpactedArticle(article.id, article.article_id, article.claim_target, in_proof))
    statement_impacted.sort(key=lambda item: item.id)
    proof_impacted.sort(key=lambda item: item.id)

    helpers: list[ImpactHelper] = []
    for name in sorted(meaning | proof):
        record = records[name]
        if record.internal or name in named or name in revised_names:
            continue
        path, line = locate(record) if locate is not None else (None, None)
        impact = "statement" if name in meaning else "proof"
        helpers.append(ImpactHelper(name, record.kind, impact, record.module, path, line, _owner(record, records, named)))

    by_id = {article.id: article for article in articles}
    impacted = (*statement_impacted, *proof_impacted)
    undeclared = sorted(item.id for item in impacted if not _reaches(item.id, revised.id, by_id))

    deprecated_users: dict[str, set[str]] = {}
    for record in records.values():
        for used in record.uses_deprecated:
            deprecated_users.setdefault(used, set()).add(record.name)
    deprecated = tuple(
        DeprecatedConstant(
            record.name,
            record.replacement,
            _users_through_internal(record.name, deprecated_users.get(record.name, set()), users, records),
            tuple(sorted(set(named.get(record.name, ())) - {revised.id})),
        )
        for record in sorted(records.values(), key=lambda item: item.name)
        if record.deprecated
    )

    # A helper is repaired under its owner's claim, so the owner is claimed too.
    owners = {by_id[helper.owner].claim_target for helper in helpers if helper.owner in by_id}
    others = {item.claim_target for item in impacted} | owners
    claim_targets = (revised.claim_target, *sorted(others - {revised.claim_target}))
    return ImpactReport(
        source_revision=source_revision,
        article=revised,
        declarations=tuple(revised_names.values()),
        statement_impacted=tuple(statement_impacted),
        proof_impacted=tuple(proof_impacted),
        helpers=tuple(helpers),
        undeclared_dependencies=tuple(undeclared),
        deprecated=deprecated,
        deprecated_unused=tuple(item.name for item in deprecated if not item.users and not item.articles),
        claim_targets=claim_targets,
    )


def _resolver(records: Mapping[str, ConstantRecord]) -> Callable[..., str | None]:
    """Resolve a user-facing name to a record: its exact name, else the private constant it names.

    Articles and ``--declaration`` name a private constant as its source does,
    without the ``_private`` prefix the probe's names carry. A name several
    private constants share is refused rather than guessed.
    """

    exact = {_name_key(name): name for name in records}
    private: dict[tuple[object, ...], list[str]] = {}
    for record in records.values():
        if record.user_name is not None:
            private.setdefault(_name_key(record.user_name), []).append(record.name)

    def resolve(name: str, context: str = "") -> str | None:
        key = _name_key(name)
        if key in exact:
            return exact[key]
        candidates = sorted(private.get(key, ()))
        if len(candidates) > 1:
            raise ImpactError(f"{context}{name} names several private declarations: {', '.join(candidates)}")
        return candidates[0] if candidates else None

    return resolve


def _reverse_closure(
    start: Iterable[str],
    users: Mapping[str, set[str]],
    *,
    admit: Callable[[str], bool] = lambda name: True,
) -> set[str]:
    """``start`` and everything that reaches it through admitted users."""

    reached = set(start)
    work = list(reached)
    while work:
        for user in users.get(work.pop(), ()):
            if user not in reached and admit(user):
                reached.add(user)
                work.append(user)
    return reached


def _owner(record: ConstantRecord, records: Mapping[str, ConstantRecord], named: Mapping[str, list[str]]) -> str | None:
    """The article naming the nearest ``parent`` ancestor of ``record``, if any."""

    seen: set[str] = set()
    parent = record.parent
    while parent is not None and parent not in seen:
        if parent in named:
            return min(named[parent])
        seen.add(parent)
        ancestor = records.get(parent)
        parent = ancestor.parent if ancestor is not None else None
    return None


def _descends_from(record: ConstantRecord, ancestor: str, records: Mapping[str, ConstantRecord]) -> bool:
    """Whether ``ancestor`` is on the ``parent`` chain of ``record``."""

    seen: set[str] = set()
    parent = record.parent
    while parent is not None and parent not in seen:
        if parent == ancestor:
            return True
        seen.add(parent)
        ancestor_record = records.get(parent)
        parent = ancestor_record.parent if ancestor_record is not None else None
    return False


def _reaches(start: str, target: str, articles: Mapping[str, ImpactArticle]) -> bool:
    """Whether ``start`` reaches ``target`` through Markdown dependencies."""

    seen = {start}
    work = [start]
    while work:
        article = articles.get(work.pop())
        for dependency in article.dependencies if article is not None else ():
            if dependency == target:
                return True
            if dependency not in seen:
                seen.add(dependency)
                work.append(dependency)
    return False


def _users_through_internal(
    name: str,
    direct: set[str],
    users: Mapping[str, set[str]],
    records: Mapping[str, ConstantRecord],
) -> tuple[str, ...]:
    """The local users of ``name``; an internal-detail user stands for its own users.

    An internal-detail user nothing else uses is kept, so that a constant is
    never reported unused while something still mentions it, unless it is a
    companion of ``name`` itself, such as its ``eq_1`` or ``_simp_1``, which
    goes when ``name`` does.
    """

    found: set[str] = set()
    seen = {name}
    work = sorted(direct)
    while work:
        user = work.pop()
        if user in seen:
            continue
        seen.add(user)
        further = users.get(user, set()) - {user}
        if records[user].internal and (further or _descends_from(records[user], name, records)):
            work.extend(sorted(further))
        else:
            found.add(user)
    return tuple(sorted(found))


# --------------------------------------------------------------------------- #
# The query
# --------------------------------------------------------------------------- #


def revision_impact(
    project_or_blueprint: str | Path,
    selector: str,
    *,
    lean_root: str | Path,
    declarations: Sequence[str] = (),
    timeout: float | None = None,
) -> ImpactReport:
    """Report what revising the Lean declarations of the selected article would affect.

    ``declarations`` replaces the article's ``lean:`` names as the revised set.
    The Lean project must be built and fresh: the probe runs through
    ``skeleton.run_probe``, with its freshness check, time limit and output bound.
    """

    source_revision, item = work_context(project_or_blueprint, selector)
    runtime = load_runtime_graph(project_or_blueprint)
    articles = {
        node.id: ImpactArticle(
            node.id,
            node.article_id,
            tuple(dict.fromkeys(target.declaration for target in node.lean_targets)),
            tuple(node.dependencies),
        )
        for node in runtime.nodes
    }
    revised = articles.get(item.node_id)
    if runtime.source_revision != source_revision or revised is None:
        raise ImpactError("the roadmap changed while it was read; rerun the command")
    names = tuple(dict.fromkeys(declarations)) or revised.declarations
    if not names:
        raise ImpactError(f"{revised.id} names no lean: declaration; pass --declaration NAME")

    root = Path(lean_root).expanduser().resolve()
    libraries = lean_libraries(root)
    modules, prefixes = project_modules(libraries)
    output = skeleton.run_probe(
        render_impact_probe(imports=modules, project_roots=prefixes),
        root,
        timeout=skeleton.DEFAULT_PROBE_TIMEOUT if timeout is None else timeout,
        label="impact probe",
    )
    return compute_impact(
        parse_impact_output(output),
        tuple(articles.values()),
        revised,
        names,
        source_revision=source_revision,
        locate=_locator(libraries, root),
    )


def _locator(libraries: tuple[LeanLibrary, ...], root: Path) -> Locator:
    """Locate a constant through the lexical source index, else by its module's file.

    The index is built on first use, since a contained revision locates nothing.
    """

    index: SourceIndex | None = None

    def locate(record: ConstantRecord) -> tuple[str | None, int | None]:
        nonlocal index
        if index is None:
            index = index_project(root)
        module_path = path_of(record.module, libraries, root)
        declaration = index.find(record.user_name or record.name)
        if declaration is not None and module_path in (None, declaration.path.as_posix()):
            return declaration.path.as_posix(), declaration.line
        return module_path, None

    return locate


def format_impact(report: ImpactReport) -> list[str]:
    """The report as short lines of text; callers escape them for the terminal."""

    names = ", ".join(report.declarations)
    lines = [
        f"Revising {names} of {_label(report.article.id, report.article.article_id)}",
        f"Graph source revision: {report.source_revision}",
    ]
    if report.contained:
        lines.append(
            f"Contained: nothing outside {report.article.id} uses {names}, so it can be revised in place."
        )
    for title, impacted in (
        ("Statement impacted", report.statement_impacted),
        ("Proof impacted", report.proof_impacted),
    ):
        if impacted:
            lines.append(f"{title}:")
            lines.extend(f"  {_label(item.id, item.article_id)}: {', '.join(item.declarations)}" for item in impacted)
    if report.helpers:
        lines.append("Helpers no article names:")
        for helper in report.helpers:
            where = helper.path or helper.module
            if helper.path and helper.line is not None:
                where = f"{where}:{helper.line}"
            owner = f"owner {helper.owner}" if helper.owner else "no owner"
            lines.append(f"  {helper.name} ({helper.kind}, {helper.impact}) {where}; {owner}")
    if report.undeclared_dependencies:
        lines.append(
            "Impacted without a Markdown dependency path to the revised article: "
            + ", ".join(report.undeclared_dependencies)
        )
    if report.deprecated:
        lines.append("Deprecated:")
        for item in report.deprecated:
            replacement = f" -> {item.replacement}" if item.replacement else ""
            uses = [f"used by {', '.join(item.users)}"] if item.users else []
            if item.articles:
                uses.append(f"named by {', '.join(item.articles)}")
            lines.append(f"  {item.name}{replacement}: {'; '.join(uses) or 'no users, safe to delete'}")
    lines.append("Claim targets: " + ", ".join(report.claim_targets))
    return lines


def _label(node_id: str, article_id: str | None) -> str:
    return f"{node_id} [{article_id}]" if article_id else node_id
