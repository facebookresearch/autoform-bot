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

Only the source directories of the indexed ``lean_lib`` targets are read, and
a target's ``globs`` select its modules as Lake does (see ``_glob``).
``--check`` finds changed, new and missing source files by reading files,
before it runs Lean.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import re
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import tomli

from .. import lean_probe, skeleton
from ..lean_probe import SkeletonError, lean_libraries
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
    dump_index,
    module_digest,
    parse_index,
    tree_digest,
)
from .locate import WorkspaceError
from .verify import CheckoutPins, _sources, checkout_pins

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


@dataclass(frozen=True, slots=True)
class LibrarySources:
    #: The indexed ``lean_lib`` targets, by name.
    libraries: tuple[str, ...]
    source_dirs: tuple[str, ...]
    #: Module name to its source file, relative to the root.
    modules: dict[str, str]
    #: Source file to its digest.
    digests: dict[str, str]


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


def library_sources(root: Path, libraries: Sequence[str] = ()) -> LibrarySources:
    """Find the modules of the indexed libraries by reading files; this starts no process for a TOML lakefile."""

    try:
        targets = lean_libraries(root)
    except SkeletonError as error:
        raise GenerateError("; ".join(error.issues)) from None
    known = {target.name: target for target in targets}
    for name in libraries:
        if name not in known:
            raise GenerateError(f"no lean_lib named {name}; the package has: {', '.join(sorted(known)) or 'none'}")
    chosen = [known[name] for name in sorted(set(libraries) or known)]
    if not chosen:
        raise GenerateError("the package declares no lean_lib")

    prefixes: dict[str, str] = {}
    # What each source directory of a library with globs builds; a library without globs builds
    # what its root imports, which is not known here, and a file it does not build fails the
    # freshness check instead.
    globs: dict[str, list[tuple[str, str]]] = {}
    for target in chosen:
        try:
            base = PurePosixPath(target.src_dir.resolve().relative_to(root).as_posix())
        except ValueError:
            raise GenerateError(f"the source directory of {target.name} is outside the package") from None
        selecting = [_glob(glob, target.name) for glob in target.globs]
        for module, mode in selecting:
            if mode != "+" and not (target.src_dir / f"{module.replace('.', '/')}.lean").is_file():
                raise GenerateError(f"lean_lib {target.name}: module {module} has no source file")
        for module_root in target.roots:
            directory = (base / module_root.replace(".", "/")).as_posix()
            prefixes[directory] = "" if base == PurePosixPath(".") else f"{base.as_posix()}/"
            if selecting:
                globs.setdefault(directory, []).extend(selecting)
    source_dirs = tuple(sorted(prefixes))
    digests = _sources(root, source_dirs, GenerateError)

    # The modules come from the files under the source directories alone: the rest of the
    # package, which may hold many more Lean files, is not read.
    modules: dict[str, str] = {}
    unbuilt: list[str] = []
    for relative in sorted(digests):
        directory = next(d for d in source_dirs if relative == f"{d}.lean" or relative.startswith(f"{d}/"))
        name = relative.removeprefix(prefixes[directory]).removesuffix(".lean").replace("/", ".")
        if not _module_name(name):
            raise GenerateError(f"{relative} is not a module name the index format holds (ASCII identifiers only)")
        modules[name] = relative
        if directory in globs and not any(_selects(glob, name) for glob in globs[directory]):
            unbuilt.append(relative)
    if unbuilt:
        raise GenerateError(
            f"{_some(unbuilt)} is under an indexed source directory but no indexed library builds it; "
            "move it, or widen the library's globs"
        )
    return LibrarySources(tuple(target.name for target in chosen), source_dirs, modules, digests)


def generate_index(
    root: str | Path,
    *,
    libraries: Sequence[str] = (),
    timeout: float | None = None,
    runner: Callable[..., str] | None = None,
) -> Generated:
    """Return the index of the built library at ``root`` without writing it."""

    directory = Path(root).expanduser().resolve()
    if not directory.is_dir():
        raise GenerateError("the package root does not exist or is not a directory")
    sources = library_sources(directory, libraries)
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
            # The probe imports every module, and `run_probe` names each one; the libraries say the same.
            raise GenerateError(
                f"the build is missing or stale; run `lake build {' '.join(sources.libraries)}` and index again"
                + (f" (Lake: {said})" if said else "")
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
    return Generated(
        index,
        data,
        left_out,
        tuple(sorted(twice, key=lambda item: item.name)),
        tuple(sorted(dropped, key=lambda item: item.name)),
    )


def write_index(root: str | Path, generated: Generated) -> Path:
    """Install the index at the package root, whole or not at all."""

    destination = Path(root).expanduser().resolve() / INDEX_FILE
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
        before = skeleton._output_identity(destination)
        stage = skeleton._stage_output(destination, generated.data.decode("utf-8"))
        replacing = True
        skeleton._replace_outputs([(destination, stage, before)])
    except (OSError, SkeletonError) as error:
        cause = "; ".join(error.issues) if isinstance(error, SkeletonError) else _reason(error)
        state = {True: "the previous one is unchanged", False: "none was written", None: "nothing was changed"}[previous]
        if replacing:
            # A failed install is rolled back, and the rollback can fail too: say what is there now.
            try:
                same = skeleton._output_identity(destination) == before
            except (OSError, SkeletonError):
                same = False
            if not same:
                state = f"{INDEX_FILE} is not the file it was before"
        raise GenerateError(f"{INDEX_FILE} could not be written: {cause}; {state}") from None
    finally:
        if stage is not None:
            with contextlib.suppress(OSError):
                skeleton._remove_output(stage)
    return destination


def check_index(
    root: str | Path,
    *,
    libraries: Sequence[str] = (),
    timeout: float | None = None,
    runner: Callable[..., str] | None = None,
) -> tuple[str, ...]:
    """Return why the index at ``root`` is not current, or nothing. Writes no file."""

    directory = Path(root).expanduser().resolve()
    if not directory.is_dir():
        raise GenerateError("the package root does not exist or is not a directory")
    try:
        data = (directory / INDEX_FILE).read_bytes()
    except FileNotFoundError:
        return (f"there is no {INDEX_FILE}",)
    except OSError as error:
        # Not stale: whether it is current cannot be told.
        raise GenerateError(f"{INDEX_FILE} exists but cannot be read: {_reason(error)}") from None
    try:
        index = parse_index(data)
    except LibraryIndexError as error:
        return (f"the index is not usable: {error}",)

    # First by reading files only, so a pull request that forgot to regenerate fails in seconds.
    sources = library_sources(directory, libraries)
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
    try:
        pins = checkout_pins(directory)
    except WorkspaceError as error:
        raise GenerateError(str(error)) from None
    if tree_digest(((module.name, module.sha256) for module in index.modules), pins.files) != index.header.tree:
        return ("the toolchain, the manifest or the Lake configuration changed since the index was generated",)

    generated = generate_index(directory, libraries=libraries, timeout=timeout, runner=runner)
    if generated.data != data.replace(b"\r\n", b"\n"):
        return ("the index differs from what generation gives for these sources",)
    return ()


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
