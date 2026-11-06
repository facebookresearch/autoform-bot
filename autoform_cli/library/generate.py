"""Generate the index a Lean library publishes, from its built environment and its sources.

Lean imports two modules that state one theorem the same way and keeps one
copy. Such a name is indexed once, under the first of its modules in name
order, with the line, statement, docstring and status of that module's
declaration: which copy Lean keeps depends on the order it loads the modules
in, and the index must not. Each such name is a warning naming the module it is
indexed under and the others, and the summary line counts them; ``--check``
does not mention them. A name that is not indexed, such as an equation lemma
two modules both generate, is no warning. A name whose first module leaves it
on ``sorry`` or declares it as an axiom is left out and counted with the
others, with a warning naming that module and the others, since one of them
may hold a whole proof. If such a name's docstring comes from
``@[inherit_doc]``, the record has none. A wanted name is private to its
module, so each module's copy is indexed.

Only the source directories of the indexed ``lean_lib`` targets are read: those
of their roots, and of their globs outside the roots. The modules are the files
there that an indexed library with the same ``srcDir`` can build, as Lake
decides (``lean_probe.builds``). A module one of them imports, as its header
reads, from a file not indexed yet brings in the library of the package that
Lake builds it with.
``library index`` and ``--check`` refuse a toolchain the probe is not verified
on before anything else, so before anything runs Lean, even
``lake translate-config``. ``--check`` then finds another probe revision,
changed pin files, and changed, new and missing source files, by reading files.
"""

from __future__ import annotations

import contextlib
import errno
import importlib.metadata
import json
import os
import re
import stat
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

# Not tomllib: Lake reads TOML 1.0, and from Python 3.15 tomllib parses 1.1 (see pyproject.toml).
import tomli

from .. import lean_probe
from .._managed_output import output_identity, remove_output, replace_outputs, stage_output
from ..lean import _IMPORT, _without_lean_comments
from ..lean_probe import LeanLibrary, SkeletonError, lean_libraries
from ..project.catalog import canonical_lean_toolchain
from .declarations import ProbeOutputError, classify, parse_probe_output, starts_with_module
from .index import (
    INDEX_FILE,
    MAX_INDEX_BYTES,
    MAX_LINE_BYTES,
    IndexDeclaration,
    IndexHeader,
    IndexModule,
    LibraryIndex,
    LibraryIndexError,
    _module_name,
    covers,
    dump_index,
    module_digest,
    parse_index,
    tree_digest,
)
from .locate import WorkspaceError
from .verify import CheckoutPins, _index_bytes, _sources, checkout_pins

LIBRARY_MARKER = "AUTOFORM_LIBRARY "
#: Raised by any change, here or in the probe, that can alter the bytes of an index.
PROBE_REVISION = 1
#: The Lean releases the probe has been run on, its use of Lean internals checked when each was added.
#: CI runs the real-library tests only on the release tests/fixtures/library-project pins.
VERIFIED_TOOLCHAINS = ("leanprover/lean4:v4.34.1", "leanprover/lean4:v4.35.0")
_LABEL = "library index probe"
_DUPLICATE = re.compile(r"import (\S+) failed, environment already contains '([^']+)' from (\S+)")
_NAMED = 5
_DETAIL = 200


def _package_name(pins: CheckoutPins) -> str:
    """The package's own name: its manifest's, else its ``lakefile.toml``'s; never its folder's."""

    if pins.package is not None:
        return pins.package
    configuration = dict(pins.files).get("lakefile.toml")
    if configuration is not None:
        try:
            name = tomli.loads(configuration.decode("utf-8")).get("name")
        except (UnicodeError, tomli.TOMLDecodeError, RecursionError, ValueError):
            name = None
        if type(name) is str and name:
            return name
    raise GenerateError("the package has no name: its lake-manifest.json sets none and neither does its lakefile.toml")


def _cut(text: str) -> str:
    return text if len(text) <= _DETAIL else text[: _DETAIL - 3] + "..."


class GenerateError(RuntimeError):
    """The library at this root cannot be indexed; the message says why."""


@dataclass(frozen=True, slots=True)
class SharedName:
    """A public name that more than one module declares, the same way each time."""

    name: str
    #: The module whose declaration is read: the first in name order of those that declare it.
    module: str
    others: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Generated:
    index: LibraryIndex
    data: bytes
    #: Usable declarations left out because they rest on ``sorry`` or another axiom.
    left_out: int
    #: The names indexed once although several modules declare them, in name order.
    shared: tuple[SharedName, ...] = ()
    #: The names several modules declare that are left out, and counted in ``left_out``, because the
    #: declaration in ``module`` rests on ``sorry`` or another axiom, in name order.
    shared_left_out: tuple[SharedName, ...] = ()
    #: What CI must build before ``--check`` when ``lake build`` of the libraries asked for leaves an
    #: indexed module unbuilt, and why; empty when it builds them all.
    build_note: str = ""


@dataclass(frozen=True, slots=True)
class LibrarySources:
    #: The indexed ``lean_lib`` targets, by name.
    libraries: tuple[str, ...]
    source_dirs: tuple[str, ...]
    #: Module name to its source file, relative to the root.
    modules: dict[str, str]
    #: Source file to its digest.
    digests: dict[str, str]
    #: Each library indexed although not named: its name, a module that imports it, and the module imported.
    joined: tuple[tuple[str, str, str], ...]
    #: The indexed modules that ``lake build`` of the libraries does not build, by name.
    unbuilt: tuple[str, ...]


def render_library_probe(modules: Sequence[str]) -> str:
    """Render the Lean program that reports every constant of ``modules``."""

    names = sorted(set(modules))
    if not names:
        raise GenerateError("the indexed libraries hold no Lean module")
    return lean_probe.render_probe_template(
        "library_probe.lean",
        imports="\n".join(f"import {module}" for module in names),
        marker=LIBRARY_MARKER,
        modules=_module_set(names),
    )


def _module_set(names: Sequence[str]) -> str:
    """The module names as one Lean string literal, one per line, which the probe reads with `String.toName`.

    An `Array Name` literal elaborates a nested term per element, and 2,808 modules exceeded Lean's
    maximum recursion depth. A module name the index accepts is ASCII identifiers joined by dots, so
    `String.toName` gives the name `Name.str` would.
    """

    return json.dumps("\n".join(names))


def library_sources(root: Path, libraries: Sequence[str] = (), *, legacy: bool = False) -> LibrarySources:
    """Find the modules of the indexed libraries by reading files; this starts no process for a TOML lakefile.

    The modules are the files under the libraries' roots and globs that an indexed library with the same
    ``srcDir`` can build, as Lake decides (``lean_probe.builds``). When the file Lake would use for an
    import is not among them, the library of the package Lake builds it with is indexed too, since a
    change there changes their records. ``legacy=True`` reads the named libraries' roots alone and joins
    nothing, which gives what Autoform indexed before 0.7.0.
    """

    try:
        targets = lean_libraries(root)
    except SkeletonError as error:
        raise GenerateError("; ".join(error.issues)) from None
    known = {target.name: target for target in targets}
    for name in libraries:
        if name not in known:
            raise GenerateError(f"no lean_lib named {name}; the package has: {', '.join(sorted(known)) or 'none'}")
    chosen = {name: known[name] for name in sorted(set(libraries) or known)}
    if not chosen:
        raise GenerateError("the package declares no lean_lib")

    imports: dict[str, tuple[str, ...]] = {}
    joined: dict[str, tuple[str, str]] = {}
    joining: dict[str, tuple[str, str]] = {}
    while True:
        directories, prefixes = _source_directories(root, chosen.values(), joined, roots_only=legacy)
        source_dirs = tuple(sorted(directories))
        try:
            digests = _sources(root, source_dirs, GenerateError)
        except GenerateError as error:
            # Only the directories of the libraries the last round joined are new to this read.
            raise GenerateError(f"{error}{''.join(_why_indexed(library, joined) for library in joining)}") from None
        # The modules come from the files under the source directories alone: the rest of the
        # package, which may hold many more Lean files, is not read.
        modules: dict[str, str] = {}
        whys: dict[str, str] = {}
        unbuildable: dict[str, tuple[str, str]] = {}
        for relative in sorted(digests):
            holder = directories[next(d for d in source_dirs if covers((d,), relative))]
            why = whys[relative] = _why_indexed(holder, joined)
            path = relative.removeprefix(prefixes[holder]).removesuffix(".lean")
            if "." in path:
                raise GenerateError(
                    f"{relative} has a dot in its file or directory name, so no module name maps to it; rename it{why}"
                )
            name = path.replace("/", ".")
            if not _module_name(name):
                raise GenerateError(
                    f"{relative} is not a module name the index format holds (ASCII identifiers only){why}"
                )
            if name in modules:
                raise GenerateError(f"{modules[name]} and {relative} both map to module {name}{why}")
            modules[name] = relative
            if not any(
                prefixes[library] == prefixes[holder] and lean_probe.builds(target, name)
                for library, target in chosen.items()
            ):
                unbuildable[relative] = (name, why)
        if legacy:
            break
        joining = {}
        indexed = digests.keys() - unbuildable.keys()
        for name, relative in modules.items():
            try:
                imported_names = _imports(root, relative, imports)
            except GenerateError as error:
                raise GenerateError(f"{error}{whys[relative]}") from None
            for imported in imported_names:
                # An import of a file a chosen library builds joins nothing, even when a later library builds it too.
                if (
                    (owner := _owner(targets, imported))
                    and owner.name not in chosen
                    and _source_file(root, owner, imported) not in indexed
                ):
                    joining.setdefault(owner.name, (name, imported))
        if not joining:
            break
        joined |= joining
        chosen |= {library: known[library] for library in joining}

    if unbuildable:
        others = sorted(
            {
                owner.name
                for name, _why in unbuildable.values()
                if (owner := _owner(targets, name)) and owner.name not in chosen
            }
        )
        raise GenerateError(
            f"{_some(list(unbuildable))} is under an indexed source directory but no indexed library builds it; "
            "move it, or widen the library's globs"
            + (f", or index {' and '.join(others)} as well" if others else "")
            + next(iter(unbuildable.values()))[1]
        )
    return LibrarySources(
        tuple(sorted(chosen)),
        source_dirs,
        modules,
        digests,
        joined=tuple((library, *joined[library]) for library in sorted(joined)),
        unbuilt=_unbuilt(root, chosen.values(), modules, imports),
    )


def _source_directories(
    root: Path, chosen: Iterable[LeanLibrary], joined: dict[str, tuple[str, str]], *, roots_only: bool
) -> tuple[dict[str, str], dict[str, str]]:
    """The source directories the chosen libraries' roots and globs give, each with the library that holds
    it, and each library's prefix, which its module names drop."""

    directories: dict[str, str] = {}
    prefixes: dict[str, str] = {}
    globbed: list[tuple[str, str]] = []
    for target in chosen:
        why = _why_indexed(target.name, joined)
        try:
            base = PurePosixPath(target.src_dir.resolve().relative_to(root).as_posix())
            globs = [_glob(glob, target.name) for glob in target.globs or ()]
        except ValueError:
            raise GenerateError(f"the source directory of {target.name} is outside the package{why}") from None
        except GenerateError as error:
            raise GenerateError(f"{error}{why}") from None
        for module, mode in globs:
            if mode != "+" and not (target.src_dir / f"{module.replace('.', '/')}.lean").is_file():
                raise GenerateError(f"lean_lib {target.name}: module {module} has no source file{why}")
        prefixes[target.name] = "" if base == PurePosixPath(".") else f"{base.as_posix()}/"
        for module_root in target.roots:
            directories[(base / module_root.replace(".", "/")).as_posix()] = target.name
        globbed.extend(((base / module.replace(".", "/")).as_posix(), target.name) for module, _mode in globs)
    # A glob outside every root adds its directory; one under a source directory adds nothing.
    for directory, holder in [] if roots_only else sorted(globbed):
        if directory not in directories and not covers(tuple(directories), directory):
            directories[directory] = holder
    return directories, prefixes


def _why_indexed(library: str, joined: dict[str, tuple[str, str]]) -> str:
    """Why a library not asked for is indexed, to append to a refusal; nothing for one asked for."""

    return f" ({library} is indexed because {' imports '.join(joined[library])})" if library in joined else ""


def _owner(targets: Sequence[LeanLibrary], module: str) -> LeanLibrary | None:
    """The library Lake builds ``module`` with: the last declared that can (``Package.findModule?``)."""

    return next((target for target in reversed(targets) if lean_probe.builds(target, module)), None)


def _source_file(root: Path, target: LeanLibrary, module: str) -> str | None:
    """The file ``target`` builds ``module`` from, relative to ``root``; None outside it."""

    try:
        return (target.src_dir.resolve() / f"{module.replace('.', '/')}.lean").relative_to(root).as_posix()
    except ValueError:
        return None


def _imports(root: Path, relative: str, cache: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """The modules the header of the file at ``relative`` imports, read as text without Lean."""

    if relative not in cache:
        try:
            text = (root / relative).read_bytes().decode("utf-8")
        except OSError as error:
            raise GenerateError(f"{relative} cannot be read ({_reason(error)})") from None
        except UnicodeError:
            raise GenerateError(f"{relative} is not valid UTF-8") from None
        names: list[str] = []
        # Lean reads imports only in the header, which ends at the first other command.
        for line in _without_lean_comments(text, mask_literals=True).splitlines():
            if found := _IMPORT.match(line):
                names.extend(found.group(1).split())
            elif line.strip() not in ("", "module", "prelude"):
                break
        cache[relative] = tuple(names)
    return cache[relative]


def _unbuilt(
    root: Path, chosen: Collection[LeanLibrary], modules: dict[str, str], cache: dict[str, tuple[str, ...]]
) -> tuple[str, ...]:
    """The indexed modules that ``lake build`` of the libraries does not build: no glob selects them,
    each root standing for itself when there are none, and no module it builds imports them."""

    def selected(target: LeanLibrary, name: str) -> bool:
        if target.globs is None:
            return name in target.roots
        return any(_selects(_glob(glob, target.name), name) for glob in target.globs)

    reached = {name for name in modules if any(selected(target, name) for target in chosen)}
    pending = sorted(reached)
    while pending:
        for imported in _imports(root, modules[pending.pop()], cache):
            if imported in modules and imported not in reached:
                reached.add(imported)
                pending.append(imported)
    return tuple(name for name in modules if name not in reached)


def _build_advice(sources: LibrarySources) -> tuple[str, str]:
    """The ``lake build`` command that builds every indexed module, and why it names more than the
    libraries asked for (nothing when it does not)."""

    reasons = [
        f"{library} is indexed because {importer} imports {imported}" for library, importer, imported in sources.joined
    ]
    if sources.unbuilt:
        reasons.append(
            f"`lake build {' '.join(sources.libraries)}` alone does not build {_some(list(sources.unbuilt))}, "
            "which no module it builds imports"
        )
    return f"lake build {' '.join((*sources.libraries, *sources.unbuilt))}", "; ".join(reasons)


def generate_index(
    root: str | Path,
    *,
    libraries: Sequence[str] = (),
    timeout: float | None = None,
    runner: Callable[..., str] | None = None,
    sources: LibrarySources | None = None,
) -> Generated:
    """Return the index of the built library at ``root`` without writing it.

    ``sources`` is what ``library_sources`` found when the caller has already looked; ``libraries`` is
    then ignored.
    """

    directory = _package_root(root)
    pins = _verified_pins(directory)
    sources = library_sources(directory, libraries) if sources is None else sources
    package = _package_name(pins)
    try:
        generator = importlib.metadata.version("autoform")
    except importlib.metadata.PackageNotFoundError:
        raise GenerateError(
            "Autoform is not installed, so the index cannot record which version wrote it; "
            "run the command from an installed Autoform"
        ) from None

    try:
        output = (runner or lean_probe.run_probe)(
            render_library_probe(tuple(sources.modules)),
            directory,
            timeout=lean_probe.DEFAULT_PROBE_TIMEOUT if timeout is None else timeout,
            label=_LABEL,
        )
    except SkeletonError as error:
        detail = "; ".join(error.issues)
        clash = _DUPLICATE.search(detail)
        if clash:
            second, name, first = clash.groups()
            raise GenerateError(_clash(first, second, name)) from None
        said = lean_probe.stale_build_said(error)
        if said is not None:
            # The probe imports every module, and the command builds each one.
            build, why = _build_advice(sources)
            raise GenerateError(
                f"the build is missing or stale; run `{build}` and index again"
                + (f" (Lake: {said})" if said else "")
                + (f"; {why}" if why else "")
            ) from None
        if detail.startswith(f"the {_LABEL} failed"):
            detail += f"\nIf the project is built, the toolchain {pins.lean_toolchain} may be the cause."
        raise GenerateError(detail) from None
    try:
        headers, constants, shared = parse_probe_output(output, LIBRARY_MARKER)
    except ProbeOutputError as error:
        raise GenerateError(str(error)) from None

    # The digests and the probe must describe one generation of the sources.
    try:
        after = _sources(directory, sources.source_dirs, GenerateError)
    except OSError as error:
        raise GenerateError(f"the sources changed while the probe ran; they cannot be read again ({error.strerror})") from None
    if after != sources.digests:
        changed = sorted(
            path
            for path in sources.digests.keys() | after.keys()
            if sources.digests.get(path) != after.get(path)
        )
        raise GenerateError(f"the sources changed while the probe ran ({_some(changed)}); rebuild and rerun")
    texts: dict[str, str] = {}
    for name, relative in sources.modules.items():
        try:
            data = (directory / relative).read_bytes()
        except OSError as error:
            raise GenerateError(f"the sources changed while the probe ran; {relative} cannot be read ({error.strerror})") from None
        if module_digest(data) != sources.digests[relative]:
            raise GenerateError(f"the sources changed while the probe ran ({relative}); rebuild and rerun")
        try:
            texts[name] = data.decode("utf-8")
        except UnicodeError:
            raise GenerateError(f"{relative} is not valid UTF-8") from None
    for name in sources.modules:
        if name not in headers:
            raise GenerateError(f"the probe did not load {name}; is it built?")

    declarations: list[IndexDeclaration] = []
    left_out = 0
    twice: list[SharedName] = []
    dropped: list[SharedName] = []
    for constant in constants:
        if constant.module not in texts:
            raise GenerateError(f"the probe reported {constant.name} in {constant.module}, which is not an indexed module")
        # Lean imports two modules that state one theorem the same way and keeps one copy; which
        # one follows from the order it loads them in. The name is indexed once, under the first
        # of its modules in name order, and the probe reports that module's copy, so the source
        # read below is the one the ranges are in.
        # A wanted placeholder is private to its module, so two modules may use one name:
        # a declaration record is keyed by its name and its module.
        several: SharedName | None = None
        if not constant.private and constant.name in shared:
            first, *others = sorted(shared[constant.name])
            if constant.module != first:
                raise GenerateError(
                    f"the probe reported {constant.name} under {constant.module}, but {', '.join([first, *others])} "
                    "declare it and it is indexed under the first of them; this is a bug in Autoform"
                )
            several = SharedName(constant.name, first, tuple(others))
        try:
            result = classify(constant, texts[constant.module])
        except ProbeOutputError as error:
            raise GenerateError(str(error)) from None
        if result == "axioms":
            left_out += 1
            if several is not None:
                dropped.append(several)
        elif not isinstance(result, str):
            if several is not None:
                twice.append(several)
            declarations.append(result)
    declarations.sort(key=lambda item: (item.name, item.module))

    modules = tuple(
        IndexModule(
            name=name,
            source_file=relative,
            sha256=sources.digests[relative],
            module_doc=headers[name],
            module_system=starts_with_module(texts[name]),
        )
        for name, relative in sorted(sources.modules.items())
    )
    header = IndexHeader(
        generator=generator,
        probe=PROBE_REVISION,
        package=package,
        source_dirs=sources.source_dirs,
        lean_toolchain=pins.lean_toolchain,
        dependencies=pins.dependencies,
        tree=tree_digest(((module.name, module.sha256) for module in modules), pins.files),
    )
    index = LibraryIndex(header, modules, tuple(declarations))
    data = dump_index(index)
    for line, declaration in zip(data.split(b"\n")[1 + len(modules) :], index.declarations):
        if len(line) > MAX_LINE_BYTES:
            raise GenerateError(
                f"the record of {declaration.name} in {declaration.module} is larger than the "
                f"{MAX_LINE_BYTES // 1024} KiB a reader accepts"
            )
    if len(data) > MAX_INDEX_BYTES:
        raise GenerateError(f"the index is larger than the {MAX_INDEX_BYTES // (1024 * 1024)} MiB a reader accepts")
    try:
        # Never hand back an index the reader would refuse.
        if parse_index(data) != index:
            raise GenerateError("the generated index does not read back as written; this is a bug in Autoform")
    except LibraryIndexError as error:
        raise GenerateError(f"the generated index is not one a reader accepts ({error}); this is a bug in Autoform") from None
    build, why = _build_advice(sources)
    return Generated(
        index,
        data,
        left_out,
        tuple(sorted(twice, key=lambda item: item.name)),
        tuple(sorted(dropped, key=lambda item: item.name)),
        f"{why}; have CI run `{build}` before checking" if why else "",
    )


def write_index(root: str | Path, generated: Generated) -> Path:
    """Install the index at the package root, whole or not at all."""

    destination = _package_root(root) / INDEX_FILE
    stage: Path | None = None
    before: tuple[int, int, str] | None = None
    #: Whether there was an index before; not known until the destination has been inspected.
    previous: bool | None = None
    replacing = False
    try:
        try:
            mode = os.lstat(destination).st_mode
        except FileNotFoundError:
            previous = False
        else:
            if not stat.S_ISREG(mode):
                raise GenerateError(f"{INDEX_FILE} exists and is not a regular file")
            previous = True
        before = output_identity(destination)
        stage = stage_output(destination, generated.data.decode("utf-8"))
        replacing = True
        replace_outputs([(destination, stage, before)])
    except (OSError, SkeletonError) as error:
        cause = "; ".join(error.issues) if isinstance(error, SkeletonError) else _reason(error)
        state = {True: "the previous one is unchanged", False: "none was written", None: "nothing was changed"}[previous]
        if replacing:
            # A failed install is rolled back, and the rollback can fail too: say what is there now.
            try:
                same = output_identity(destination) == before
            except (OSError, SkeletonError):
                same = False
            if not same:
                state = f"{INDEX_FILE} is not the file it was before"
        raise GenerateError(f"{INDEX_FILE} could not be written: {cause}; {state}") from None
    finally:
        if stage is not None:
            with contextlib.suppress(OSError):
                remove_output(stage)
    return destination


def check_index(
    root: str | Path,
    *,
    libraries: Sequence[str] = (),
    timeout: float | None = None,
    runner: Callable[..., str] | None = None,
    warnings: list[str] | None = None,
) -> tuple[str, ...]:
    """Return why the index at ``root`` is not current, or nothing.

    Writes no index, but the probe's freshness check rewrites Lake's ``.hash`` files. What the verdict
    leaves unsaid, such as a library the index should also cover, is appended to ``warnings``.
    """

    directory = _package_root(root)
    # Before anything is called stale: regenerating could not fix it.
    pins = _verified_pins(directory)
    try:
        # Read as every project reads it: a symbolic link, a FIFO or an oversized file is no index.
        data = _index_bytes(directory / INDEX_FILE)
    except FileNotFoundError:
        return (f"there is no {INDEX_FILE}",)
    except OSError as error:
        # A link (refused as ELOOP) or a file that is not regular; any other failure is not stale:
        # whether the index is current cannot be told.
        if error.errno not in (None, errno.ELOOP):
            raise GenerateError(f"{INDEX_FILE} exists but cannot be read: {_reason(error)}") from None
        return (f"{INDEX_FILE} is not a regular file, so no project reads it",)
    try:
        index = parse_index(data)
    except LibraryIndexError as error:
        return (f"the index is not usable: {error}",)

    # Then by reading files only, so a pull request that forgot to regenerate fails in seconds.
    if index.header.probe != PROBE_REVISION:
        return (f"the index was made by probe revision {index.header.probe}, and this Autoform's is {PROBE_REVISION}",)
    if tree_digest(((module.name, module.sha256) for module in index.modules), pins.files) != index.header.tree:
        return ("the toolchain, the manifest or the Lake configuration changed since the index was generated",)
    sources, warning = _sources_to_check(directory, libraries, index.header.source_dirs)
    if warning and warnings is not None:
        warnings.append(warning)
    recorded = {module.source_file: module.sha256 for module in index.modules}
    changed = sorted(p for p in recorded.keys() & sources.digests.keys() if recorded[p] != sources.digests[p])
    reasons = [
        f"{label}: {_some(paths)}"
        for label, paths in (
            ("changed since the index was generated", changed),
            ("not in the index", sorted(sources.digests.keys() - recorded.keys())),
            ("in the index but missing", sorted(recorded.keys() - sources.digests.keys())),
        )
        if paths
    ]
    if reasons:
        return tuple(reasons)

    generated = generate_index(directory, timeout=timeout, runner=runner, sources=sources).index
    # An Autoform release that writes the same records leaves the index current, whatever version it records.
    generated = replace(generated, header=replace(generated.header, generator=index.header.generator))
    if dump_index(generated) == data.replace(b"\r\n", b"\n"):
        return ()
    return (_what_differs(index, generated),)


def _sources_to_check(
    directory: Path, libraries: Sequence[str], source_dirs: tuple[str, ...]
) -> tuple[LibrarySources, str]:
    """The sources an index recording ``source_dirs`` is checked against, and a warning or nothing.

    An index made before globs' directories and imported libraries were read keeps its verdict (#325):
    it is checked against the legacy read, with a warning naming what it leaves out.
    """

    try:
        sources = library_sources(directory, libraries)
    except GenerateError as error:
        sources, refusal = None, error
    else:
        if set(sources.source_dirs) == set(source_dirs):
            return sources, ""
        refusal = None
    try:
        legacy = library_sources(directory, libraries, legacy=True)
    except GenerateError:
        legacy = None
    if legacy is None or set(legacy.source_dirs) != set(source_dirs):
        if sources is None:
            raise refusal
        return sources, ""
    if sources is None:
        return legacy, f"regenerating the index will be refused: {refusal}"
    left_out = sorted(sources.digests.keys() - legacy.digests.keys())
    if not left_out:
        return legacy, ""
    build, why = _build_advice(sources)
    return legacy, (
        f"the index leaves out {_some(left_out)}, so a change there leaves it current; regenerate it"
        + (f", and have CI run `{build}` before checking; {why}" if why else "")
    )


def _package_root(root: str | Path) -> Path:
    try:
        directory = Path(root).expanduser().resolve()
    except RuntimeError as error:
        # `~name` for a user this system does not know.
        raise GenerateError(f"the package root cannot be resolved: {error}") from None
    if not directory.is_dir():
        raise GenerateError("the package root does not exist or is not a directory")
    return directory


def _verified_pins(directory: Path) -> CheckoutPins:
    """The package's pin files, once its toolchain is one the probe is verified on; nothing here starts a process."""

    try:
        pins = checkout_pins(directory)
    except WorkspaceError as error:
        raise GenerateError(str(error)) from None
    if pins.lean_toolchain is None:
        raise GenerateError("the package has no lean-toolchain")
    if canonical_lean_toolchain(pins.lean_toolchain) not in VERIFIED_TOOLCHAINS:
        raise GenerateError(
            f"the package pins {_cut(pins.lean_toolchain)}, but the library probe is verified only on "
            + ", ".join(VERIFIED_TOOLCHAINS)
        )
    return pins


def _what_differs(recorded: LibraryIndex, generated: LibraryIndex) -> str:
    """Name the records that differ between ``recorded`` and what generation gives."""

    ours, theirs = recorded.header.as_dict(), generated.header.as_dict()
    fields = [key for key in theirs if ours[key] != theirs[key]]
    modules = sorted({module.name for module in set(recorded.modules) ^ set(generated.modules)})
    declarations = sorted({item.name for item in set(recorded.declarations) ^ set(generated.declarations)})
    parts = [f"the header's {', '.join(fields)}"] if fields else []
    for what, names in (("module", modules), ("declaration", declarations)):
        if names:
            parts.append(f"{len(names)} {what}{'' if len(names) == 1 else 's'} ({_some(names)})")
    if not parts:
        return "the index holds the records generation gives, but not in the bytes it writes"
    return "generation gives other records for these sources: " + "; ".join(parts)


def _glob(glob: str, library: str) -> tuple[str, str]:
    """Read a Lake module glob: ``M`` one module, ``M.*`` it and its submodules, ``M.+`` its submodules only."""

    module, mode = (glob[:-2], glob[-1]) if glob.endswith((".*", ".+")) else (glob, "")
    if not _module_name(module):
        raise GenerateError(
            f"lean_lib {library}: the module glob {glob!r} is not `M`, `M.*` or `M.+` over a plain module name"
        )
    return module, mode


def _selects(glob: tuple[str, str], name: str) -> bool:
    """Whether Lake's ``Glob.matches`` holds: a prefix is a prefix of whole name components."""

    module, mode = glob
    return mode != "+" if name == module else bool(mode) and name.startswith(f"{module}.")


def _clash(first: str, second: str, name: str) -> str:
    # Lean's own refusal to import two modules that declare one name in two ways.
    return f"{first} and {second} both declare {name}; a public name must come from one module, so the library cannot be indexed"


def _reason(error: OSError) -> str:
    """The operating system's reason for a failure, without the path."""

    return error.strerror or str(error) or type(error).__name__


def _some(paths: list[str]) -> str:
    return ", ".join(paths[:_NAMED]) + (", ..." if len(paths) > _NAMED else "")


__all__ = [
    "LIBRARY_MARKER",
    "PROBE_REVISION",
    "VERIFIED_TOOLCHAINS",
    "GenerateError",
    "Generated",
    "LibrarySources",
    "SharedName",
    "check_index",
    "generate_index",
    "library_sources",
    "render_library_probe",
    "write_index",
]
