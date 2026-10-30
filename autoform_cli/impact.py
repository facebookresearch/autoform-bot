"""Report what revising an article's Lean declarations would affect.

``autoform work impact`` answers the question a worker must settle before
changing declarations other work may build on: which articles now state
something else, whose proofs may stop elaborating, which helpers no article
names sit in between, and whether the Markdown roadmap records those
dependencies. The answer is read from the elaborated environment by a Lean
probe rather than from source text, so uses that only automation such as
``simp`` introduces are seen too.

A Markdown path reaches the revised declarations when it ends at the revised
article, at an article that names one of them, or, when none names it, at one
of its owners; missing dependency paths are counted the same way. Project
locality is an exact inventory of regular repository source modules, never a
namespace-prefix guess. Lake builds every module an ``M.*`` or ``M.+`` glob
finds even when nothing imports it, so the probe refuses such a module it did
not load instead of silently leaving out what the module uses.

``work impact`` snapshots and rereads the roadmap around the probe. It retains
one bound Lean source generation, derives the module inventory and locations
from that generation's captured bytes, then recaptures through the same binding
and refuses any content or source-identity change. ``work import-impact`` takes
the loaded module graph from Lean and binds every node to its captured root
source and configured root-package artifact, so a dependency cannot shadow it.
Under ``--module-scope import-closure`` a file is a candidate when its text
contains every component of a candidate's name, repeated until no new file
matches (:func:`import_closure`), and Lean loads what the candidates import.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from . import lean_probe
from ._tree_snapshot import TreeSnapshotError, _normalized_name
from .claims import author_claim_key
from .lean import IndexedSourceSnapshot, LeanSourceError, bind_project_source_snapshot, resolve_lean_root
from .lean_probe import (
    LeanLibrary,
    SkeletonError,
    lean_libraries,
    lean_name_components,
    lean_name_parts,
    lean_requests,
)
from .runtime import load_runtime_graph
from .work import select_work_item

IMPACT_SCHEMA = "autoform-impact/v1"
MODULE_IMPORT_IMPACT_SCHEMA = "autoform-module-import-impact/v1"
MODULE_IMPORT_SCOPE = "loaded-root-library-graph"
IMPORT_CLOSURE_SCOPE = "import-closure"

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
    """One loaded root module, its direct local imports, and its loaded artifact."""

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
    whatever that value mentions could not be traced. ``allow_empty`` accepts a
    run with no project-local constants, which module evidence can still answer.
    """

    records: dict[str, ConstantRecord] = {}
    # Not `splitlines`: Lean's JSON printer leaves U+0085, U+2028 and U+2029 unescaped.
    for line in text.split("\n"):
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

    Only direct imports recorded by Lean are exposed. Requiring every named
    local import to have its own record makes the result a closed graph rather
    than a collection of independently plausible edges.
    """

    records: dict[str, ModuleEvidence] = {}
    # Not `splitlines`: Lean's JSON printer leaves U+0085, U+2028 and U+2029 unescaped.
    for line in text.split("\n"):
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
        parts = lean_name_parts(name)
    except SkeletonError:
        return (name,)
    return tuple((part, not quoted and part.isascii() and part.isdigit()) for part, quoted in parts)


# --------------------------------------------------------------------------- #
# Project modules and the probe
# --------------------------------------------------------------------------- #

#: Module name components the probe can import without quoting.
_MODULE_COMPONENT = re.compile(r"[A-Za-z_][A-Za-z0-9_'!?]*")
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
        sources, unimportable = _source_modules(library, snapshot)
        local_modules.update(sources)
        # A glob also selects modules nothing can import; they are refused
        # below like any other non-plain name, not dropped.
        selected = {**unimportable, **sources}
        for glob in library.globs or library.roots:
            base, mode = _glob(glob, library)
            if mode != "+":
                if base not in sources:
                    raise SkeletonError([f"lean_lib {library.name}: module {base} has no source file"])
                modules.add(base)
            if mode:
                descendants = sorted(module for module in selected if module.startswith(f"{base}."))
                for module in descendants:
                    if not all(_MODULE_COMPONENT.fullmatch(part) for part in module.split(".")):
                        raise SkeletonError(
                            [
                                f"lean_lib {library.name}: cannot import {selected[module].as_posix()}; "
                                "the impact probe supports plain module names only"
                            ]
                        )
                modules.update(descendants)
    if not modules:
        raise SkeletonError(["the Lake configuration selects no modules"])
    return tuple(sorted(modules)), tuple(sorted(local_modules))


def _source_modules(
    library: LeanLibrary, snapshot: IndexedSourceSnapshot
) -> tuple[dict[str, Path], dict[str, Path]]:
    """Map source modules to captured repository-relative paths for one library.

    The second map holds sources whose module name would contain ``»``. Lake
    may build them, but no import can name them, so they are never local.
    """

    try:
        prefix = library.src_dir.relative_to(snapshot.index.root)
    except ValueError as exc:
        raise SkeletonError([f"lean_lib {library.name}: srcDir is outside the captured Lean root"]) from exc
    found: dict[str, Path] = {}
    unimportable: dict[str, Path] = {}
    for path, _data in snapshot.source_files:
        try:
            relative = path.relative_to(prefix) if prefix.parts else path
        except ValueError:
            continue
        if len(relative.parts) - 1 > _MAX_SOURCE_DEPTH:
            raise SkeletonError(
                [f"lean_lib {library.name}: source tree exceeds {_MAX_SOURCE_DEPTH} directory levels"]
            )
        # Not with_suffix(""), which rejects the stem `.` of `..lean` on 3.13.
        parts = (*relative.parts[:-1], relative.stem)
        # Quote components that are not plain identifiers, so `Basic copy.lean`
        # is the module `«Basic copy»` that Lean reports.
        module = ".".join(part if _MODULE_COMPONENT.fullmatch(part) else f"«{part}»" for part in parts)
        # A component containing `»` has no quoted spelling, so nothing can
        # import that module. Quoting it anyway keeps a dotted stem one
        # component, so a glob selects it exactly when Lake does.
        if any("»" in part for part in parts):
            unimportable.setdefault(module, path)
            continue
        if not module:
            continue
        found.setdefault(module, path)
    return found, unimportable


def _module_source_paths(
    libraries: Sequence[LeanLibrary],
    snapshot: IndexedSourceSnapshot,
    *,
    strict: bool = False,
) -> dict[str, str]:
    """Map modules to source paths; ``strict`` keeps selected modules only and requires a bijection."""

    paths: dict[str, str] = {}
    modules_by_path: dict[str, str] = {}
    for library in libraries:
        for module, path in _source_modules(library, snapshot)[0].items():
            relative = path.as_posix()
            if not strict:
                paths.setdefault(module, relative)
                continue
            # Lake builds only modules a library selects, so a file under a
            # nested srcDir is one module although the outer srcDir holds it,
            # and a file no library selects claims no module.
            if not _selects(library, module):
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


def _may_import(data: bytes, module: str) -> bool:
    """Whether ``data`` could import ``module``: it contains each component, however the import spells it."""

    return all(part.encode("utf-8") in data for part, _quoted in lean_name_parts(module))


def import_closure(
    module: str,
    sources: Iterable[tuple[str, bytes]],
    outside: Iterable[tuple[str, bytes]],
) -> set[str]:
    """``module`` and every root-package module that may import it, transitively.

    ``sources`` pairs each module name with one file's bytes; a name two
    libraries' files share joins when any of them may import the closure.
    The test is textual, so it can admit a module that does not import one in
    the closure but never misses one that does. A file outside the captured
    inventory that may import the closure has no module name to load, so the
    narrowed scope is refused rather than incomplete.
    """

    sources = list(sources)
    closure = {module}
    work = [module]
    while work:
        target = work.pop()
        for name, data in sources:
            if name not in closure and _may_import(data, target):
                closure.add(name)
                work.append(name)
    firsts = {lean_name_parts(name)[0][0].encode("utf-8") for name in closure}
    for path, data in outside:
        if any(first in data for first in firsts):
            for name in sorted(closure):
                if _may_import(data, name):
                    raise ImpactError(
                        f"{path} may import {name} but is outside the captured root-package sources; "
                        "rerun with the default --module-scope"
                    )
    return closure


_RERUN_DEFAULT_SCOPE = "; rerun with the default --module-scope"


def _raise(error: OSError) -> None:
    raise ImpactError(f"cannot read {error.filename}{_RERUN_DEFAULT_SCOPE}") from error


def _outside_sources(
    root: Path, libraries: Sequence[LeanLibrary], inventoried: Collection[Path]
) -> Iterator[tuple[str, bytes]]:
    """Every ``.lean`` file the captured inventory lacks where Lake can find a module.

    Lake finds a root-package module only in a library's ``srcDir``, under the
    first component of one of its roots or globs (``LeanLib.isLocalModule``),
    and a dependency's module in its package directory. Nothing else under
    ``root``, such as build output, is ever loaded, so it is not read; nor is
    a ``.lake`` in a package, or a nested checkout, which the captured
    inventory leaves out too.
    """

    # Each top is a directory or file to read, with the depth of the packages it holds.
    tops: dict[Path, int | None] = {}
    for library in libraries:
        bases = (*library.roots, *(_glob(glob, library)[0] for glob in library.globs))
        try:
            firsts: set[str] | None = {_normalized_name(lean_name_parts(base)[0][0]) for base in bases}
        except SkeletonError:
            firsts = None  # a name this parser cannot read may be in any entry
        try:
            entries = sorted(os.listdir(library.src_dir))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as error:
            raise ImpactError(f"cannot read {library.src_dir}{_RERUN_DEFAULT_SCOPE}") from error
        for entry in entries:
            # Matched loosely, since a case-insensitive volume finds `Demo/` under `demo/`.
            if entry != ".git" and (firsts is None or _normalized_name(entry.removesuffix(".lean")) in firsts):
                tops.setdefault(library.src_dir / entry, None)
    manifest_path = root / "lake-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    except (OSError, ValueError) as error:
        raise ImpactError(f"cannot read {manifest_path}{_RERUN_DEFAULT_SCOPE}") from error
    dirs: list[tuple[object, int]] = [(".lake/packages", 1)]
    if isinstance(manifest, dict):
        dirs.append((manifest.get("packagesDir"), 1))
        packages = manifest.get("packages")
        if isinstance(packages, list):
            dirs += [
                (package.get("dir"), 0)
                for package in packages
                if isinstance(package, dict) and package.get("type") == "path"
            ]
    for entry, packages_at in dirs:
        if isinstance(entry, str):
            top = Path(os.path.normpath(root / entry))
            if top.is_dir():
                tops.setdefault(top, packages_at)
    seen: set[str] = set()
    for top, packages_at in tops.items():
        walked = top.is_dir()
        # An unreadable directory raises rather than hiding a file that may import the closure.
        # A top that is no directory is a library's `<root>.lean`, or a link to read and refuse.
        listing = os.walk(top, onerror=_raise, followlinks=True) if walked else [(str(top.parent), [], [top.name])]
        for directory, names, files in listing:
            folder = Path(directory)
            if walked:
                real = os.path.realpath(directory)
                depth = len(folder.relative_to(top).parts)
                # A nested checkout, such as a worktree with its own `.lake`, is another
                # source tree, as the captured inventory treats it; a package root is a clone.
                # A module in one, such as a library's Git submodule, goes unread.
                checkout = (packages_at is None or depth > packages_at) and (".git" in names or ".git" in files)
                if real in seen or checkout:
                    names[:] = []
                    continue
                seen.add(real)
                # In a package, `.lake` holds build output and packages Lake does not use;
                # a dependency whose srcDir lies inside it goes unread.
                skipped = {".git", ".lake"} if packages_at is not None else {".git"}
                names[:] = [name for name in names if name not in skipped]
            # Only `.lean` names cost a path, since build trees hold many other files.
            base = folder.relative_to(root) if folder.is_relative_to(root) else folder
            for file in files:
                if not file.endswith(".lean") or base / file in inventoried:
                    continue
                path = folder / file
                try:
                    data = path.read_bytes()
                except OSError as error:
                    raise ImpactError(f"cannot read {path}{_RERUN_DEFAULT_SCOPE}") from error
                yield (base / file).as_posix(), data


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

    # Lean prints `H.α` for the source module the inventory spells `H.«α»`, so
    # names meet by component rather than by spelling, as in the locator.
    source_paths = _module_source_paths(libraries, snapshot, strict=True)
    expected_paths = {_name_key(module): relative for module, relative in source_paths.items()}
    expected_artifacts = _module_artifact_paths(libraries, source_paths)
    for module, record in evidence.items():
        key = _name_key(module)
        relative = expected_paths.get(key)
        if relative is None:
            raise SkeletonError(
                [f"the impact probe loaded {module} without a captured root-project source"]
            )
        expected = root.joinpath(*Path(relative).parts)
        if not _same_file(Path(record.source_path), expected):
            raise SkeletonError(
                [
                    f"project module {module} resolved to {record.source_path}, "
                    f"expected captured root source {expected}"
                ]
            )
        expected_artifact = expected_artifacts[key]
        if not _same_file(Path(record.olean_path), expected_artifact):
            raise SkeletonError(
                [
                    f"project module {module} loaded artifact {record.olean_path}, "
                    f"expected root artifact {expected_artifact}"
                ]
            )
    return evidence


def _same_file(found: Path, expected: Path) -> bool:
    """Whether Lean's real path ``found`` names the file at ``expected``.

    ``expected`` keeps the spelling of ``--lean-root``, so on a case-insensitive
    file system it can differ from the on-disk spelling Lean reports.
    """

    if found == expected:
        return True
    try:
        return os.path.samefile(found, expected)
    except OSError:
        return False


def _module_artifact_paths(
    libraries: Sequence[LeanLibrary], source_paths: Mapping[str, str]
) -> dict[tuple[object, ...], Path]:
    """Return the one root-package artifact path permitted for each module, by name key."""

    artifact_dir = _root_artifact_dir(libraries)
    artifacts: dict[tuple[object, ...], Path] = {}
    for module in source_paths:
        *package, stem = (part for part, _quoted in lean_name_parts(module))
        # Not with_suffix, which would cut the dotted stem of `«a.b»`.
        artifacts[_name_key(module)] = artifact_dir.joinpath(*package, f"{stem}.olean").resolve()
    return artifacts


def _root_artifact_dir(libraries: Sequence[LeanLibrary]) -> Path:
    """Return the one Lean artifact directory the root libraries build into."""

    artifact_dirs = {library.artifact_dir for library in libraries}
    if None in artifact_dirs or len(artifact_dirs) != 1:
        raise SkeletonError(["cannot identify one root-package Lean artifact directory"])
    artifact_dir = next(iter(artifact_dirs))
    assert artifact_dir is not None
    return artifact_dir


def _glob_directories(libraries: Sequence[LeanLibrary]) -> list[tuple[Path, str, str]]:
    """The directory each ``M.*`` or ``M.+`` glob walks, with ``M`` and the glob."""

    directories: list[tuple[Path, str, str]] = []
    for library in libraries:
        for glob in library.globs:
            base, mode = _glob(glob, library)
            if mode:
                directories.append((library.src_dir.joinpath(*base.split(".")), base, glob))
    return directories


def _selects(library: LeanLibrary, module: str) -> bool:
    """Whether ``library`` holds ``module``, as Lake's ``LeanLib.isLocalModule`` decides."""

    if any(module == root or module.startswith(f"{root}.") for root in library.roots):
        return True
    for glob in library.globs:
        base, mode = _glob(glob, library)
        if (module == base and mode != "+") or (mode and module.startswith(f"{base}.")):
            return True
    return False


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
    selected_modules: Iterable[str] = (),
    artifact_dir: Path | None = None,
    glob_directories: Iterable[tuple[Path, str, str]] = (),
    emit_module_evidence: bool = False,
) -> str:
    """Render the Lean program that records every project-local constant.

    Given ``artifact_dir``, the root package's Lean artifact directory, the
    probe refuses a root-package module the captured inventory lacks: a loaded
    module outside ``project_modules`` whose OLean lies under ``artifact_dir``,
    or a module outside ``selected_modules`` (every module a library's globs or
    roots select) that it finds by walking a ``glob_directories`` entry. Each
    entry is the directory of an ``M.*`` or ``M.+`` glob, with ``M`` and the
    glob. ``emit_module_evidence`` also records every loaded project module's
    source, artifact and direct imports.
    """

    modules = sorted(set(imports))
    if not modules:
        raise SkeletonError(["refusing to render an impact probe with no imports"])
    walks = [
        [str(directory), list(lean_name_components(base)), glob] for directory, base, glob in glob_directories
    ]
    return _impact_template().format(
        imports="\n".join(f"import {module}" for module in modules),
        marker=IMPACT_MARKER,
        module_marker=IMPACT_MODULE_MARKER,
        check_inventory="false" if artifact_dir is None else "true",
        emit_module_evidence="true" if emit_module_evidence else "false",
        artifact_dir=json.dumps(str(artifact_dir or ""), ensure_ascii=False),
        glob_directories=json.dumps(json.dumps(walks, ensure_ascii=False), ensure_ascii=False),
        output_env=lean_probe.PROBE_OUTPUT_ENV,
        output_limit=lean_probe.DEFAULT_PROBE_OUTPUT_LIMIT,
        project_modules=lean_requests(tuple(sorted(set(project_modules)))),
        selected_modules=lean_requests(tuple(sorted(set(selected_modules)))),
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
    """Reverse-import scope of changing one project module's imports."""

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
    module_scope: str = MODULE_IMPORT_SCOPE

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
        """Whether the scope stays inside the selected article.

        It holds when the selected article is affected, nothing else is a claim
        target (no other article names or owns an affected declaration, and no
        unowned helper is affected), and no other article has unresolved
        declarations whose scope is unknown, and the scope is not
        ``import-closure``, whose ownership over-reports.
        """

        return (
            self.module_scope != IMPORT_CLOSURE_SCOPE
            and self.selected_affected
            and not self.affected_claim_targets
            and all(article.id == self.article.id for article in self.unresolved_articles)
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": MODULE_IMPORT_IMPACT_SCHEMA,
            "source_revision": self.source_revision,
            "lean_source_revision": self.lean_source_revision,
            "build_revision": self.build_revision,
            "module_graph_revision": self.module_graph_revision,
            "module_scope": self.module_scope,
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
    module_scope: str = MODULE_IMPORT_SCOPE,
    declaration_modules: Mapping[str, str] | None = None,
) -> ModuleImportImpactReport:
    """Return the reverse-import scope of changing ``module`` imports.

    Imports change the environment of the edited module and every project
    module that imports it, transitively, which covers what an added import
    can affect. Removing an import can also drop modules from the build,
    transitively, when only imports bring them in, as under a library's
    roots; this scope does not list them. It never refuses the edit merely
    because the scope is larger than one article. ``declaration_modules`` maps
    a name to the module whose source declares it; an unresolved name it places
    in an unaffected module is not reported.
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
    touched: dict[str, set[str]] = {}
    unresolved_articles: list[ImpactedArticle] = []
    for article in articles:
        resolved_names: list[str] = []
        unresolved_names: list[str] = []
        for declaration in article.declarations:
            resolved = resolve(declaration, f"{article.id}: lean: ")
            if resolved is None:
                place = (declaration_modules or {}).get(declaration)
                if not article.mathlib and (place is None or place in affected_set):
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
        touched[article.id] = {records[resolved].module for resolved in resolved_names} & affected_set

    by_key: dict[tuple[object, ...], set[str]] = {}
    for article in articles:
        for declaration in article.declarations:
            by_key.setdefault(_name_key(declaration), set()).add(article.id)

    def owners(record: ConstantRecord) -> tuple[str, ...]:
        if module_scope != IMPORT_CLOSURE_SCOPE:
            return _owners(record, records, named)
        # A parent declared in a module this scope did not load is missing from
        # the chain, so every article naming a prefix owns the record.
        key = _name_key(record.user_name or record.name)
        return tuple(sorted(set().union(*(by_key.get(key[:length], ()) for length in range(1, len(key) + 1)))))

    # An article also owns, through the parent chain, the helpers it names no
    # declaration for, such as a structure's API lemmas in a later module.
    for record in records.values():
        if record.module in affected_set and record.name not in named:
            for owner in owners(record):
                touched[owner].add(record.module)
    affected_articles = tuple(
        ModuleImpactedArticle(article.id, article.article_id, article.claim_target, tuple(sorted(touched[article.id])))
        for article in sorted(articles, key=lambda article: article.id)
        if touched[article.id]
    )
    unresolved_articles.sort(key=lambda article: article.id)

    module_claim_target = _module_claim_target(module)
    # Helpers keep the parent-chain rule under either scope: a narrowed chain
    # is only shorter, so every helper the default scope reports keeps its key.
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
        affected_articles=affected_articles,
        unresolved_articles=tuple(unresolved_articles),
        affected_unowned_helpers=affected_helpers,
        affected_claim_targets=affected_claim_targets,
        claim_targets=claim_targets,
        module_scope=module_scope,
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

    for parent in _ancestors(record, records):
        if parent in named:
            return tuple(sorted(set(named[parent])))
    return ()


def _ancestors(record: ConstantRecord, records: Mapping[str, ConstantRecord]) -> Iterator[str]:
    """The ``parent`` chain of ``record``, nearest first, stopping where it repeats."""

    seen: set[str] = set()
    parent = record.parent
    while parent is not None and parent not in seen:
        yield parent
        seen.add(parent)
        ancestor_record = records.get(parent)
        parent = ancestor_record.parent if ancestor_record is not None else None


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
        if records[user].internal and (further or name in _ancestors(records[user], records)):
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
        return lean_probe.project_control_snapshot(root)
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
    declaration_modules: Mapping[str, str] | None = None


def _load_impact_evidence(
    project_or_blueprint: str | Path,
    selector: str,
    *,
    lean_root: str | Path,
    timeout: float | None,
    require_modules: bool,
    require_default_declarations: bool,
    module: str | None = None,
    module_scope: str = MODULE_IMPORT_SCOPE,
) -> _ImpactEvidence:
    """Load one freshness-bound roadmap, source, module, and Lean environment."""

    runtime = load_runtime_graph(project_or_blueprint)
    source_revision = runtime.source_revision
    item = select_work_item(runtime, selector)
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
    selected = articles_by_id[item.node_id]
    if require_default_declarations and not selected.declarations:
        raise ImpactError(f"{selected.id} names no lean: declaration; pass --declaration NAME")

    try:
        root = resolve_lean_root(lean_root)
    except LeanSourceError as error:
        raise ImpactError(str(error)) from error
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
        selected_modules = imports
        declaration_modules = None
        if module_scope == IMPORT_CLOSURE_SCOPE:
            if module not in local_modules:
                raise ImpactError(f"not a project module: {module}")
            captured = dict(source_snapshot.source_files)
            sources = [
                (name, captured[path])
                for library in libraries
                for name, path in _source_modules(library, source_snapshot)[0].items()
            ]
            closure = import_closure(module, sources, _outside_sources(root, libraries, set(captured)))
            paths = {path: name for name, path in _module_source_paths(libraries, source_snapshot, strict=True).items()}
            index = source_snapshot.index
            # A name the narrowed probe did not load is unresolved; the index
            # says whether its source lies in a module the edit can affect.
            declaration_modules = {
                name: paths[declaration.path.as_posix()]
                for name, declaration in index.declarations.items()
                if name not in index.duplicates and declaration.path.as_posix() in paths
            }
            imports = tuple(name for name in imports if name in closure)
            if not imports:
                raise ImpactError(f"not a loaded project module: {module}")
        try:
            output = lean_probe.run_probe(
                render_impact_probe(
                    imports=imports,
                    project_modules=local_modules,
                    selected_modules=selected_modules,
                    artifact_dir=_root_artifact_dir(libraries),
                    glob_directories=_glob_directories(libraries),
                    emit_module_evidence=require_modules,
                ),
                root,
                timeout=lean_probe.DEFAULT_PROBE_TIMEOUT if timeout is None else timeout,
                label="impact probe",
            )
        except SkeletonError as error:
            said = lean_probe.stale_build_said(error)
            if said is None:
                raise
            if module_scope == IMPORT_CLOSURE_SCOPE:
                names = shlex.join(imports)
            else:
                # The probe imports every root library's modules, which can be thousands; the libraries say the same.
                names = shlex.join(sorted({library.name for library in libraries}))
            checked = f"{len(imports)} module{'' if len(imports) == 1 else 's'}"
            raise SkeletonError(
                [
                    f"{lean_probe.STALE_BUILD}{names}` before running the impact probe "
                    f"({checked}" + (f"; Lake: {said})" if said else ")")
                ]
            ) from None
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
        declaration_modules=declaration_modules,
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
    ``lean_probe.run_probe``, with its freshness check, time limit and output bound.
    """

    evidence = _load_impact_evidence(
        project_or_blueprint,
        selector,
        lean_root=lean_root,
        timeout=timeout,
        require_modules=False,
        require_default_declarations=not declarations,
    )
    # Without --declaration, _load_impact_evidence refused an article that names none.
    names = tuple(dict.fromkeys(declarations)) or evidence.selected.declarations
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
    module_scope: str = MODULE_IMPORT_SCOPE,
) -> ModuleImportImpactReport:
    """Report, without blocking it, the reverse-import scope of an import edit."""

    if module_scope not in (MODULE_IMPORT_SCOPE, IMPORT_CLOSURE_SCOPE):
        raise ImpactError(f"unknown module scope: {module_scope}")
    evidence = _load_impact_evidence(
        project_or_blueprint,
        selector,
        lean_root=lean_root,
        timeout=timeout,
        require_modules=True,
        require_default_declarations=False,
        module=module,
        module_scope=module_scope,
    )
    return compute_module_import_impact(
        evidence.records,
        evidence.modules,
        evidence.articles,
        evidence.selected,
        module,
        source_revision=evidence.source_revision,
        lean_source_revision=evidence.lean_source_revision,
        module_scope=module_scope,
        declaration_modules=evidence.declaration_modules,
    )


def _locator(
    libraries: tuple[LeanLibrary, ...],
    snapshot: IndexedSourceSnapshot,
) -> Locator:
    """Locate a constant in its compiled module's file, with the line from the index, else through the index alone.

    The compiled module says which of a name's declarations a record is, so a
    private one beside a public twin is still located.
    """

    def locate(record: ConstantRecord) -> tuple[str | None, int | None]:
        module_path = module_paths.get(_name_key(record.module))
        name = record.user_name or record.name
        if module_path is None:
            declaration = snapshot.index.find(name)
            return (None, None) if declaration is None else (declaration.path.as_posix(), declaration.line)
        line = next(
            (item.line for item in snapshot.index.occurrences(name) if item.path.as_posix() == module_path), None
        )
        return module_path, line

    module_paths = {_name_key(module): path for module, path in _module_source_paths(libraries, snapshot).items()}
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
        f"Module scope: {report.module_scope}",
        f"Module claim target: {report.module_claim_target} (key {report.module_claim_key})",
        "Affected project modules: " + ", ".join(report.affected_modules),
        "Removing an import can also drop modules that only imports bring into the build; "
        "this reverse-import scope does not list them.",
    ]
    if not report.selected_affected:
        lines.append(
            "Warning: the selected article has no built declaration in this reverse-import scope."
        )
    other_articles = tuple(
        article for article in report.affected_articles if article.id != report.article.id
    )
    if report.article_local:
        lines.append("Article scope: no other article and no unowned helper is in the reverse-import scope.")
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
            "Check other workers, live claims, or semantic changes for: "
            + _keyed(report.affected_claim_targets, report.affected_claim_keys)
        )
    lines.append("Claim targets of the import edit: " + _keyed(report.claim_targets, report.claim_keys))
    return lines


def _label(node_id: str, article_id: str | None) -> str:
    return f"{node_id} [{article_id}]" if article_id else node_id


def _keyed(targets: Sequence[str], keys: Sequence[str]) -> str:
    return ", ".join(f"{target} (key {key})" for target, key in zip(targets, keys))
