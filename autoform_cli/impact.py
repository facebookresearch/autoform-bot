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

import hashlib
import json
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from . import skeleton
from ._tree_snapshot import TreeSnapshotError
from .claims import author_claim_key
from .lean import IndexedSourceSnapshot, bind_project_source_snapshot
from .runtime import load_runtime_graph
from .skeleton import LeanLibrary, SkeletonError, _lean_name, _lean_name_parts, lean_libraries
from .work import work_context

IMPACT_SCHEMA = "autoform-impact/v1"
MODULE_IMPORT_IMPACT_SCHEMA = "autoform-module-import-impact/v1"
MODULE_IMPORT_SCOPE = "loaded-root-library-graph"

#: Every line the impact probe wants read back starts with this marker.
IMPACT_MARKER = "AUTOFORM_IMPACT "
#: Module-import evidence is a separate stream so old constant-record readers
#: continue to ignore it.
IMPACT_MODULE_MARKER = "AUTOFORM_IMPACT_MODULE "

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


@dataclass(frozen=True, slots=True)
class ModuleEvidence:
    """One loaded root module, its direct local imports, and loaded artifact."""

    module: str
    source_path: str
    direct_local_imports: tuple[str, ...] = ()
    olean_path: str = ""


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


def parse_impact_output(text: str, *, allow_empty: bool = False) -> dict[str, ConstantRecord]:
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
    if not records and not allow_empty:
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


def parse_module_evidence(text: str) -> dict[str, ModuleEvidence]:
    """Return the probe's complete, strictly validated loaded-local graph.

    Only direct imports recorded by Lean are exposed.  Requiring every named
    local import to have its own record makes the result a closed graph rather
    than a collection of independently plausible edges.
    """

    records: dict[str, ModuleEvidence] = {}
    for line in text.splitlines():
        if not line.startswith(IMPACT_MODULE_MARKER):
            continue
        try:
            payload = json.loads(line[len(IMPACT_MODULE_MARKER) :])
        except json.JSONDecodeError as exc:
            raise SkeletonError([f"the impact probe emitted invalid module-evidence JSON: {exc}"]) from exc
        record = _module_evidence(payload)
        if record.module in records:
            raise SkeletonError([f"the impact probe emitted module evidence for {record.module} twice"])
        records[record.module] = record

    if not records:
        raise SkeletonError(["the impact probe emitted no project-module evidence"])

    for record in records.values():
        if record.module in record.direct_local_imports:
            raise SkeletonError([f"the impact probe reported {record.module} importing itself"])
        unknown = sorted(set(record.direct_local_imports) - records.keys())
        if unknown:
            raise SkeletonError(
                [f"the impact probe reported {record.module} importing unknown local modules: {unknown}"]
            )

    return records


def _module_evidence(payload: object) -> ModuleEvidence:
    if not isinstance(payload, dict) or set(payload) != {
        "module",
        "source_path",
        "olean_path",
        "direct_local_imports",
    }:
        raise SkeletonError(["the impact probe emitted module evidence with unexpected fields"])
    module = payload["module"]
    source_path = payload["source_path"]
    olean_path = payload["olean_path"]
    imports = payload["direct_local_imports"]
    if (
        not isinstance(module, str)
        or not module
        or not isinstance(source_path, str)
        or not source_path
        or "\x00" in source_path
        or not isinstance(olean_path, str)
        or not olean_path
        or "\x00" in olean_path
        or not isinstance(imports, list)
        or not all(isinstance(item, str) and item for item in imports)
    ):
        raise SkeletonError(["the impact probe emitted malformed module evidence"])
    if len(imports) != len(set(imports)):
        raise SkeletonError(
            [f"the impact probe emitted duplicate direct imports for project module {module}"]
        )
    return ModuleEvidence(module, source_path, tuple(imports), olean_path)


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
_MAX_PROJECT_MODULES = 10_000
_MAX_SOURCE_DEPTH = 64


def project_modules(
    libraries: Sequence[LeanLibrary],
    snapshot: IndexedSourceSnapshot,
    *,
    strict_module_ownership: bool = False,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the probe imports and exact repository-owned module names.

    A library's ``globs`` select its modules as Lake's ``Glob`` does: ``M`` is
    one module, ``M.*`` the module and its submodules, ``M.+`` its strict
    submodules. A library without globs builds its roots. Both selection and
    locality come from the snapshot's exact source-file generation rather than
    a second walk of the live tree.
    """

    if strict_module_ownership:
        # A module claim must identify exactly one source file, and one source
        # file must not get two module claims through overlapping srcDirs.
        _module_source_paths(libraries, snapshot, strict=True)
    modules: set[str] = set()
    local_modules: set[str] = set()
    for library in libraries:
        sources = _source_modules(library, snapshot)
        local_modules.update(sources)
        for glob in library.globs or library.roots:
            base, mode = _glob(glob, library)
            if mode != "+":
                if base not in sources:
                    raise SkeletonError([f"lean_lib {library.name}: module {base} has no source file"])
                modules.add(base)
            if mode:
                descendants = sorted(module for module in sources if module.startswith(f"{base}."))
                for module in descendants:
                    if not all(_MODULE_COMPONENT.fullmatch(part) for part in module.split(".")):
                        raise SkeletonError(
                            [
                                f"lean_lib {library.name}: cannot import {sources[module].as_posix()}; "
                                "the impact probe supports plain module names only"
                            ]
                        )
                modules.update(descendants)
    if not modules:
        raise SkeletonError(["the Lake configuration selects no modules"])
    return tuple(sorted(modules)), tuple(sorted(local_modules))


def _source_modules(library: LeanLibrary, snapshot: IndexedSourceSnapshot) -> dict[str, Path]:
    """Map source modules to captured repository-relative paths for one library."""

    try:
        prefix = library.src_dir.relative_to(snapshot.index.root)
    except ValueError as exc:
        raise SkeletonError([f"lean_lib {library.name}: srcDir is outside the captured Lean root"]) from exc
    found: dict[str, Path] = {}
    for path, _data in snapshot.source_files:
        try:
            relative = path.relative_to(prefix) if prefix.parts else path
        except ValueError:
            continue
        if len(relative.parts) - 1 > _MAX_SOURCE_DEPTH:
            raise SkeletonError(
                [f"lean_lib {library.name}: source tree exceeds {_MAX_SOURCE_DEPTH} directory levels"]
            )
        module = ".".join(relative.with_suffix("").parts)
        if not module:
            continue
        found.setdefault(module, path)
        if len(found) > _MAX_PROJECT_MODULES:
            raise SkeletonError(
                [f"lean_lib {library.name}: source tree exceeds {_MAX_PROJECT_MODULES} Lean modules"]
            )
    return found


def _module_source_paths(
    libraries: Sequence[LeanLibrary],
    snapshot: IndexedSourceSnapshot,
    *,
    strict: bool = False,
) -> dict[str, str]:
    """Map modules to source paths, optionally requiring a bijection."""

    paths: dict[str, str] = {}
    modules_by_path: dict[str, str] = {}
    for library in libraries:
        for module, path in _source_modules(library, snapshot).items():
            relative = path.as_posix()
            if not strict:
                paths.setdefault(module, relative)
                continue
            existing_path = paths.get(module)
            if existing_path is not None and existing_path != relative:
                raise SkeletonError(
                    [
                        f"project module {module} maps to more than one source: "
                        f"{existing_path}, {relative}"
                    ]
                )
            existing_module = modules_by_path.get(relative)
            if existing_module is not None and existing_module != module:
                raise SkeletonError(
                    [
                        f"project source {relative} maps to more than one module: "
                        f"{existing_module}, {module}"
                    ]
                )
            paths[module] = relative
            modules_by_path[relative] = module
    return paths


def _validated_module_evidence(
    text: str,
    *,
    root: Path,
    libraries: Sequence[LeanLibrary],
    snapshot: IndexedSourceSnapshot,
    required_modules: Collection[str],
) -> dict[str, ModuleEvidence]:
    """Bind Lean's loaded module graph to the captured root source paths."""

    evidence = parse_module_evidence(text)
    missing = sorted(set(required_modules) - evidence.keys())
    if missing:
        raise SkeletonError([f"the impact probe omitted required project modules: {missing}"])

    expected_paths = _module_source_paths(libraries, snapshot, strict=True)
    expected_artifacts = _module_artifact_paths(libraries, expected_paths)
    for module, record in evidence.items():
        relative = expected_paths.get(module)
        if relative is None:
            raise SkeletonError(
                [f"the impact probe loaded {module} without a captured root-project source"]
            )
        expected = root.joinpath(*Path(relative).parts)
        if Path(record.source_path) != expected:
            raise SkeletonError(
                [
                    f"project module {module} resolved to {record.source_path}, "
                    f"expected captured root source {expected}"
                ]
            )
        expected_artifact = expected_artifacts[module]
        if Path(record.olean_path) != expected_artifact:
            raise SkeletonError(
                [
                    f"project module {module} loaded artifact {record.olean_path}, "
                    f"expected root artifact {expected_artifact}"
                ]
            )
    return evidence


def _module_artifact_paths(
    libraries: Sequence[LeanLibrary], source_paths: Mapping[str, str]
) -> dict[str, Path]:
    """Return the one root-package artifact path permitted for each module."""

    artifact_dirs = {library.artifact_dir for library in libraries}
    if None in artifact_dirs or len(artifact_dirs) != 1:
        raise SkeletonError(["cannot identify one root-package Lean artifact directory"])
    artifact_dir = next(iter(artifact_dirs))
    assert artifact_dir is not None
    return {
        module: artifact_dir.joinpath(*module.split(".")).with_suffix(".olean").resolve()
        for module in source_paths
    }


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


def _impact_template() -> str:
    """The Lean impact probe source, kept under probes/ beside this module."""

    return (Path(__file__).parent / "probes" / "impact_probe.lean").read_text(encoding="utf-8")


def render_impact_probe(
    *,
    imports: Iterable[str],
    project_modules: Iterable[str],
    include_module_evidence: bool = False,
) -> str:
    """Render the Lean program that records every project-local constant."""

    modules = sorted(set(imports))
    if not modules:
        raise SkeletonError(["refusing to render an impact probe with no imports"])
    return _impact_template().format(
        imports="\n".join(f"import {module}" for module in modules),
        marker=IMPACT_MARKER,
        module_marker=IMPACT_MODULE_MARKER,
        emit_module_evidence="true" if include_module_evidence else "false",
        output_env=skeleton.PROBE_OUTPUT_ENV,
        output_limit=skeleton.DEFAULT_PROBE_OUTPUT_LIMIT,
        project_modules=", ".join(_lean_name(name) for name in sorted(set(project_modules))),
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
    statement_dependencies: tuple[str, ...] = ()
    stated: bool = False
    mathlib: bool = False

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
class ModuleImpactedArticle:
    """One article with a local declaration in an import-affected module."""

    id: str
    article_id: str | None
    claim_target: str
    modules: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "article_id": self.article_id,
            "claim_target": self.claim_target,
            "modules": list(self.modules),
        }


@dataclass(frozen=True, slots=True)
class ModuleImpactedHelper:
    """One unowned helper in an import-affected module and its stable claim."""

    name: str
    module: str
    claim_target: str

    @property
    def claim_key(self) -> str:
        return author_claim_key(self.claim_target)

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "module": self.module,
            "claim_target": self.claim_target,
            "claim_key": self.claim_key,
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
    owners: tuple[str, ...]
    #: Every owner's claim target, or one key derived from the helper's own
    #: name, so all revisions touching it contend for the same claim set.
    claim_targets: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "impact": self.impact,
            "module": self.module,
            "path": self.path,
            "line": self.line,
            "owners": list(self.owners),
            "claim_targets": list(self.claim_targets),
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
    lean_source_revision: str
    build_revision: str
    article: ImpactArticle
    declarations: tuple[str, ...]
    statement_impacted: tuple[ImpactedArticle, ...]
    proof_impacted: tuple[ImpactedArticle, ...]
    helpers: tuple[ImpactHelper, ...]
    #: Impacted articles with no Markdown dependency path to the revised
    #: declarations: to the revised article, or to an article naming one of
    #: them or, when none names it, owning it.
    undeclared_dependencies: tuple[str, ...]
    #: Stated articles other than Mathlib ones whose Markdown statement rests on
    #: the revised declarations, through statement edges only, that are not
    #: statement-impacted: their Lean may inline a revised definition's body
    #: instead of naming it.
    unused_statement_dependencies: tuple[str, ...]
    deprecated: tuple[DeprecatedConstant, ...]
    deprecated_unused: tuple[str, ...]
    claim_targets: tuple[str, ...]

    @property
    def contained(self) -> bool:
        """Whether nothing outside the revised article is impacted.

        A helper the revised article owns, such as a structure's generated
        constructor or recursor, is repaired under that article's claim, so it
        does not count; any other helper, owned or not, does, and so does a
        revised declaration that belongs to another article or to none, or a
        stated article, other than a Mathlib one, whose Markdown statement rests
        on the revised declarations, even when its Lean shows no use. The
        revision is contained exactly when its only claim target is the revised
        article's.
        """

        return self.claim_targets == (self.article.claim_target,)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": IMPACT_SCHEMA,
            "source_revision": self.source_revision,
            "lean_source_revision": self.lean_source_revision,
            "build_revision": self.build_revision,
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
            "unused_statement_dependencies": list(self.unused_statement_dependencies),
            "deprecated": [item.as_dict() for item in self.deprecated],
            "deprecated_unused": list(self.deprecated_unused),
            "claim_targets": list(self.claim_targets),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ModuleImportImpactReport:
    """Conservative loaded-build scope of changing one project module's imports."""

    source_revision: str
    lean_source_revision: str
    build_revision: str
    module_graph_revision: str
    article: ImpactArticle
    module: str
    module_claim_target: str
    affected_modules: tuple[str, ...]
    affected_articles: tuple[ModuleImpactedArticle, ...]
    unresolved_articles: tuple[ImpactedArticle, ...]
    affected_unowned_helpers: tuple[ModuleImpactedHelper, ...]
    affected_claim_targets: tuple[str, ...]
    claim_targets: tuple[str, ...]

    @property
    def selected_affected(self) -> bool:
        return any(article.id == self.article.id for article in self.affected_articles)

    @property
    def module_claim_key(self) -> str:
        return author_claim_key(self.module_claim_target)

    @property
    def affected_claim_keys(self) -> tuple[str, ...]:
        return tuple(author_claim_key(target) for target in self.affected_claim_targets)

    @property
    def claim_keys(self) -> tuple[str, ...]:
        return tuple(author_claim_key(target) for target in self.claim_targets)

    @property
    def article_local(self) -> bool:
        return self.selected_affected and {article.id for article in self.affected_articles} <= {
            self.article.id
        } and not any(article.id != self.article.id for article in self.unresolved_articles)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": MODULE_IMPORT_IMPACT_SCHEMA,
            "source_revision": self.source_revision,
            "lean_source_revision": self.lean_source_revision,
            "build_revision": self.build_revision,
            "module_graph_revision": self.module_graph_revision,
            "module_scope": MODULE_IMPORT_SCOPE,
            "article": {
                "id": self.article.id,
                "article_id": self.article.article_id,
                "claim_target": self.article.claim_target,
            },
            "module": self.module,
            "module_claim_target": self.module_claim_target,
            "module_claim_key": self.module_claim_key,
            "selected_affected": self.selected_affected,
            "article_local": self.article_local,
            "affected_modules": list(self.affected_modules),
            "affected_articles": [article.as_dict() for article in self.affected_articles],
            "unresolved_articles": [article.as_dict() for article in self.unresolved_articles],
            "affected_unowned_helpers": [helper.as_dict() for helper in self.affected_unowned_helpers],
            "affected_claim_targets": list(self.affected_claim_targets),
            "affected_claim_keys": list(self.affected_claim_keys),
            "claim_targets": list(self.claim_targets),
            "claim_keys": list(self.claim_keys),
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
    lean_source_revision: str = "",
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

    by_id = {article.id: article for article in articles}
    helpers: list[ImpactHelper] = []
    for name in sorted(meaning | proof):
        record = records[name]
        if record.internal or name in named or name in revised_names:
            continue
        path, line = locate(record) if locate is not None else (None, None)
        impact = "statement" if name in meaning else "proof"
        owners = _owners(record, records, named)
        targets = _claim_targets_for(name, owners, by_id)
        helpers.append(
            ImpactHelper(name, record.kind, impact, record.module, path, line, owners, targets)
        )

    # A revised declaration belongs to the articles naming it or, when none
    # does, to its owners, so a Markdown path to any of them reaches the
    # revision as surely as a path to the revised article does.
    anchors = {revised.id}
    for name in revised_names:
        anchors.update(named.get(name) or _owners(records[name], records, named))
    impacted = (*statement_impacted, *proof_impacted)
    undeclared = sorted(
        item.id for item in impacted if item.id not in anchors and not _reaches(item.id, anchors, by_id)
    )
    # The probe sees only names, so a dependent whose Lean inlines a revised
    # definition's body instead of naming it would keep its statement under the
    # old meaning; its Markdown statement dependency is the only trace. A
    # Mathlib article's statement is a Mathlib declaration, which cannot use the
    # revised one, and the loader refuses to retract it, so it is left out.
    statement_ids = {item.id for item in statement_impacted}
    unused = sorted(
        article.id
        for article in articles
        if article.stated
        and not article.mathlib
        and article.id != revised.id
        and article.id not in statement_ids
        and _reaches(article.id, anchors, by_id, statement_only=True)
    )

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

    # A helper is repaired under every owning article's claim; an unowned
    # helper contributes the key derived from its own name.
    # A revised declaration no article names is claimed the same way.
    others = (
        {item.claim_target for item in impacted}
        | {by_id[article_id].claim_target for article_id in unused}
        | {target for helper in helpers for target in helper.claim_targets}
    )
    for name in revised_names:
        if name not in named:
            others.update(_claim_targets_for(name, _owners(records[name], records, named), by_id))
    claim_targets = (revised.claim_target, *sorted(others - {revised.claim_target}))
    return ImpactReport(
        source_revision=source_revision,
        lean_source_revision=lean_source_revision,
        build_revision=_build_revision(records),
        article=revised,
        declarations=tuple(revised_names.values()),
        statement_impacted=tuple(statement_impacted),
        proof_impacted=tuple(proof_impacted),
        helpers=tuple(helpers),
        undeclared_dependencies=tuple(undeclared),
        unused_statement_dependencies=tuple(unused),
        deprecated=deprecated,
        deprecated_unused=tuple(item.name for item in deprecated if not item.users and not item.articles),
        claim_targets=claim_targets,
    )


def compute_module_import_impact(
    records: Mapping[str, ConstantRecord],
    modules: Mapping[str, ModuleEvidence],
    articles: Sequence[ImpactArticle],
    selected: ImpactArticle,
    module: str,
    *,
    source_revision: str,
    lean_source_revision: str = "",
) -> ModuleImportImpactReport:
    """Return the conservative local scope of changing ``module`` imports.

    Imports change the environment of the edited module and every project
    module that imports it, transitively.  This reports that scope; it never
    refuses the edit merely because the scope is larger than one article.
    """

    if module not in modules:
        raise ImpactError(f"not a loaded project module: {module}")

    importers: dict[str, set[str]] = {}
    for evidence in modules.values():
        for imported in evidence.direct_local_imports:
            importers.setdefault(imported, set()).add(evidence.module)
    affected_modules = tuple(sorted(_reverse_closure((module,), importers)))
    affected_set = set(affected_modules)
    resolve = _resolver(records)
    named: dict[str, list[str]] = {}
    affected_articles: list[ModuleImpactedArticle] = []
    unresolved_articles: list[ImpactedArticle] = []
    for article in articles:
        resolved_names: list[str] = []
        unresolved_names: list[str] = []
        for declaration in article.declarations:
            resolved = resolve(declaration, f"{article.id}: lean: ")
            if resolved is None:
                if not article.mathlib:
                    unresolved_names.append(declaration)
            else:
                resolved_names.append(resolved)
        if unresolved_names:
            unresolved_articles.append(
                ImpactedArticle(
                    article.id,
                    article.article_id,
                    article.claim_target,
                    tuple(unresolved_names),
                )
            )
        for resolved in resolved_names:
            named.setdefault(resolved, []).append(article.id)
        article_modules = {records[resolved].module for resolved in resolved_names}
        touched = tuple(sorted(article_modules & affected_set))
        if touched:
            affected_articles.append(
                ModuleImpactedArticle(
                    article.id,
                    article.article_id,
                    article.claim_target,
                    touched,
                )
            )
    affected_articles.sort(key=lambda article: article.id)
    unresolved_articles.sort(key=lambda article: article.id)

    module_claim_target = _module_claim_target(module)
    article_by_id = {article.id: article for article in articles}
    affected_helpers = tuple(
        ModuleImpactedHelper(record.name, record.module, _helper_claim_target(record.name))
        for record in sorted(records.values(), key=lambda record: record.name)
        if record.module in affected_set
        and not record.internal
        and record.name not in named
        and not _owners(record, records, named)
    )
    affected_claim_targets = tuple(
        sorted(
            {
                *(
                    article.claim_target
                    for article in affected_articles
                    if article.claim_target != selected.claim_target
                ),
                *(helper.claim_target for helper in affected_helpers),
                *(
                    article_by_id[owner].claim_target
                    for record in records.values()
                    if record.module in affected_set and record.name not in named
                    for owner in _owners(record, records, named)
                    if owner in article_by_id and owner != selected.id
                ),
            }
        )
    )
    # The import edit itself owns only its selected article and module header.
    # Other article claims are reported separately: they block only when live,
    # or become required when validation shows that their content must change.
    claim_targets = (selected.claim_target, module_claim_target)
    return ModuleImportImpactReport(
        source_revision=source_revision,
        lean_source_revision=lean_source_revision,
        build_revision=_build_revision(records),
        module_graph_revision=_module_graph_revision(modules),
        article=selected,
        module=module,
        module_claim_target=module_claim_target,
        affected_modules=affected_modules,
        affected_articles=tuple(affected_articles),
        unresolved_articles=tuple(unresolved_articles),
        affected_unowned_helpers=affected_helpers,
        affected_claim_targets=affected_claim_targets,
        claim_targets=claim_targets,
    )


def _build_revision(records: Mapping[str, ConstantRecord]) -> str:
    """Hash the exact normalized Lean environment records behind a report."""

    payload = {name: asdict(records[name]) for name in sorted(records)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _module_graph_revision(modules: Mapping[str, ModuleEvidence]) -> str:
    """Hash the exact loaded local module nodes and direct import edges."""

    payload = {
        module: list(modules[module].direct_local_imports)
        for module in sorted(modules)
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _helper_claim_target(name: str) -> str:
    """A stable raw claim target for a helper no article owns."""

    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:48] or "declaration"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return f"lean/{slug}-{digest}"


def _module_claim_target(module: str) -> str:
    """A stable raw claim target for edits to one Lean module header."""

    slug = re.sub(r"[^a-z0-9-]+", "-", module.lower()).strip("-")[:48] or "module"
    digest = hashlib.sha256(module.encode("utf-8")).hexdigest()[:16]
    return f"lean-module/{slug}-{digest}"


def _claim_targets_for(
    name: str,
    owners: Iterable[str],
    articles: Mapping[str, ImpactArticle],
) -> tuple[str, ...]:
    """Every owner's claim target, or a stable key when no article owns ``name``."""

    targets = {articles[owner].claim_target for owner in owners if owner in articles}
    return tuple(sorted(targets)) or (_helper_claim_target(name),)


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


def _owners(
    record: ConstantRecord,
    records: Mapping[str, ConstantRecord],
    named: Mapping[str, list[str]],
) -> tuple[str, ...]:
    """Every article naming the nearest ``parent`` ancestor of ``record``."""

    seen: set[str] = set()
    parent = record.parent
    while parent is not None and parent not in seen:
        if parent in named:
            return tuple(sorted(set(named[parent])))
        seen.add(parent)
        ancestor = records.get(parent)
        parent = ancestor.parent if ancestor is not None else None
    return ()


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


def _reaches(
    start: str, targets: Collection[str], articles: Mapping[str, ImpactArticle], *, statement_only: bool = False
) -> bool:
    """Whether ``start`` reaches one of ``targets`` through Markdown dependencies, or only statement ones."""

    seen = {start}
    work = [start]
    while work:
        article = articles.get(work.pop())
        edges = () if article is None else article.statement_dependencies if statement_only else article.dependencies
        for dependency in edges:
            if dependency in targets:
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


def _project_controls(root: Path) -> tuple[tuple[str, tuple[int, str] | None], ...]:
    """Read the Lake inputs that select the environment as one stable snapshot."""

    try:
        return skeleton._project_control_snapshot(root)
    except SkeletonError as exc:
        raise ImpactError(
            "the Lake project controls could not be read consistently; rebuild and rerun the command"
        ) from exc


@dataclass(frozen=True, slots=True)
class _ImpactEvidence:
    source_revision: str
    lean_source_revision: str
    articles: tuple[ImpactArticle, ...]
    selected: ImpactArticle
    records: Mapping[str, ConstantRecord]
    modules: Mapping[str, ModuleEvidence]
    locate: Locator


def _load_impact_evidence(
    project_or_blueprint: str | Path,
    selector: str,
    *,
    lean_root: str | Path,
    timeout: float | None,
    require_modules: bool,
    require_default_declarations: bool,
) -> _ImpactEvidence:
    """Load one freshness-bound roadmap, source, module, and Lean environment."""

    source_revision, item = work_context(project_or_blueprint, selector)
    runtime = load_runtime_graph(project_or_blueprint)
    articles_by_id = {
        node.id: ImpactArticle(
            node.id,
            node.article_id,
            tuple(dict.fromkeys(target.declaration for target in node.lean_targets)),
            tuple(node.dependencies),
            tuple(node.statement_dependencies),
            node.status.stated,
            node.mathlib,
        )
        for node in runtime.nodes
    }
    selected = articles_by_id.get(item.node_id)
    if runtime.source_revision != source_revision or selected is None:
        raise ImpactError("the roadmap changed while it was read; rerun the command")
    if require_default_declarations and not selected.declarations:
        raise ImpactError(f"{selected.id} names no lean: declaration; pass --declaration NAME")

    root = Path(lean_root).expanduser().resolve()
    controls = _project_controls(root)
    with bind_project_source_snapshot(root) as (bound_sources, source_snapshot):
        libraries = lean_libraries(root)
        if _project_controls(root) != controls:
            raise ImpactError(
                "the Lake project controls changed while library inventory was read; "
                "rebuild and rerun the command"
            )
        imports, local_modules = project_modules(
            libraries,
            source_snapshot,
            strict_module_ownership=require_modules,
        )
        output = skeleton.run_probe(
            render_impact_probe(
                imports=imports,
                project_modules=local_modules,
                include_module_evidence=require_modules,
            ),
            root,
            timeout=skeleton.DEFAULT_PROBE_TIMEOUT if timeout is None else timeout,
            label="impact probe",
        )
        latest = load_runtime_graph(project_or_blueprint)
        if latest.source_revision != source_revision:
            raise ImpactError("the roadmap changed while the impact probe ran; rerun the command")
        if _project_controls(root) != controls:
            raise ImpactError(
                "the Lake project controls changed while the impact probe ran; rebuild and rerun the command"
            )
        try:
            bound_sources.verify()
            latest_sources = bound_sources.capture()
        except (OSError, TreeSnapshotError) as exc:
            raise ImpactError(
                "the Lean sources changed while the impact probe ran; rebuild and rerun the command"
            ) from exc
        if latest_sources.generation_revision != source_snapshot.generation_revision:
            raise ImpactError("the Lean sources changed while the impact probe ran; rebuild and rerun the command")

    records = parse_impact_output(output, allow_empty=require_modules)
    module_evidence: Mapping[str, ModuleEvidence] = {}
    if require_modules:
        required_modules = {*imports, *(record.module for record in records.values())}
        module_evidence = _validated_module_evidence(
            output,
            root=root,
            libraries=libraries,
            snapshot=source_snapshot,
            required_modules=required_modules,
        )

    return _ImpactEvidence(
        source_revision=source_revision,
        lean_source_revision=source_snapshot.revision,
        articles=tuple(articles_by_id.values()),
        selected=selected,
        records=records,
        modules=module_evidence,
        locate=_locator(libraries, source_snapshot),
    )


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

    evidence = _load_impact_evidence(
        project_or_blueprint,
        selector,
        lean_root=lean_root,
        timeout=timeout,
        require_modules=False,
        require_default_declarations=not declarations,
    )
    names = tuple(dict.fromkeys(declarations)) or evidence.selected.declarations
    if not names:
        raise ImpactError(f"{evidence.selected.id} names no lean: declaration; pass --declaration NAME")
    return compute_impact(
        evidence.records,
        evidence.articles,
        evidence.selected,
        names,
        source_revision=evidence.source_revision,
        lean_source_revision=evidence.lean_source_revision,
        locate=evidence.locate,
    )


def module_import_impact(
    project_or_blueprint: str | Path,
    selector: str,
    module: str,
    *,
    lean_root: str | Path,
    timeout: float | None = None,
) -> ModuleImportImpactReport:
    """Report, without blocking it, the conservative scope of an import edit."""

    evidence = _load_impact_evidence(
        project_or_blueprint,
        selector,
        lean_root=lean_root,
        timeout=timeout,
        require_modules=True,
        require_default_declarations=False,
    )
    return compute_module_import_impact(
        evidence.records,
        evidence.modules,
        evidence.articles,
        evidence.selected,
        module,
        source_revision=evidence.source_revision,
        lean_source_revision=evidence.lean_source_revision,
    )


def _locator(
    libraries: tuple[LeanLibrary, ...],
    snapshot: IndexedSourceSnapshot,
) -> Locator:
    """Locate a constant through one captured index, else by its captured module path."""

    def locate(record: ConstantRecord) -> tuple[str | None, int | None]:
        module_path = module_paths.get(record.module)
        declaration = snapshot.index.find(record.user_name or record.name)
        if declaration is not None and module_path in (None, declaration.path.as_posix()):
            return declaration.path.as_posix(), declaration.line
        return module_path, None

    module_paths = _module_source_paths(libraries, snapshot)
    return locate


def format_impact(report: ImpactReport) -> list[str]:
    """The report as short lines of text; callers escape them for the terminal."""

    names = ", ".join(report.declarations)
    lines = [
        f"Revising {names} of {_label(report.article.id, report.article.article_id)}",
        f"Graph source revision: {report.source_revision}",
        f"Lean source revision: {report.lean_source_revision or 'unbound'}",
        f"Lean build revision: {report.build_revision}",
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
            if len(helper.owners) == 1:
                ownership = f"owner {helper.owners[0]}"
            elif helper.owners:
                ownership = "owners " + ", ".join(helper.owners)
            else:
                ownership = "no owner"
            claims = "claims " + ", ".join(helper.claim_targets)
            lines.append(
                f"  {helper.name} ({helper.kind}, {helper.impact}) {where}; {ownership}; {claims}"
            )
    if report.undeclared_dependencies:
        lines.append(
            "Impacted without a Markdown dependency path to the revised declarations: "
            + ", ".join(report.undeclared_dependencies)
        )
    if report.unused_statement_dependencies:
        lines.append(
            "Statement dependents in Markdown that are not statement impacted: "
            + ", ".join(report.unused_statement_dependencies)
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


def format_module_import_impact(report: ModuleImportImpactReport) -> list[str]:
    """Render a short, explicitly permissive module-import impact report."""

    lines = [
        f"Import edit allowed in {report.module} for {_label(report.article.id, report.article.article_id)}.",
        f"Graph source revision: {report.source_revision}",
        f"Lean source revision: {report.lean_source_revision or 'unbound'}",
        f"Lean build revision: {report.build_revision}",
        f"Module graph revision: {report.module_graph_revision}",
        f"Module scope: {MODULE_IMPORT_SCOPE}",
        f"Module claim target: {report.module_claim_target} (key {report.module_claim_key})",
        "Affected project modules: " + ", ".join(report.affected_modules),
    ]
    if not report.selected_affected:
        lines.append(
            "Warning: the selected article has no built declaration in this reverse-import scope."
        )
    other_articles = tuple(
        article for article in report.affected_articles if article.id != report.article.id
    )
    if report.article_local:
        lines.append("Article scope: no other article names a declaration in the reverse-import scope.")
    elif other_articles:
        lines.append("Affected articles:")
        lines.extend(
            f"  {_label(article.id, article.article_id)}: {', '.join(article.modules)}"
            for article in other_articles
        )
    else:
        detail = (
            "none beyond the selected article"
            if report.selected_affected
            else "none in the loaded reverse-import scope"
        )
        lines.append(f"Affected built articles: {detail}.")
    if report.affected_unowned_helpers:
        lines.append("Potentially affected unowned helpers:")
        lines.extend(
            f"  {helper.name} ({helper.module}): {helper.claim_target} (key {helper.claim_key})"
            for helper in report.affected_unowned_helpers
        )
    if report.unresolved_articles:
        lines.append("Articles with unresolved local declarations (scope unknown):")
        lines.extend(
            f"  {_label(article.id, article.article_id)}: {', '.join(article.declarations)}"
            for article in report.unresolved_articles
        )
    if report.affected_claim_targets:
        lines.append(
            "Check active claims or semantic changes for: "
            + ", ".join(
                f"{target} (key {key})"
                for target, key in zip(
                    report.affected_claim_targets,
                    report.affected_claim_keys,
                )
            )
        )
    lines.append(
        "Required claims for the import edit: "
        + ", ".join(
            f"{target} (key {key})"
            for target, key in zip(report.claim_targets, report.claim_keys)
        )
    )
    return lines


def _label(node_id: str, article_id: str | None) -> str:
    return f"{node_id} [{article_id}]" if article_id else node_id
