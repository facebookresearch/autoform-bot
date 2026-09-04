"""Validate and resolve structured Lean imports for one Lake project."""

from __future__ import annotations

import os
import re
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from servers import ProjectFingerprint, lean_project_fingerprint

_MAX_DISCOVERY_OUTPUT_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_PROCESS_KILL_WAIT_SECONDS = 5.0
_READ_CHUNK_BYTES = 64 * 1024
_MODULE_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_']*")
_SOURCE_HEADER_MODULE = re.compile(
    r"[A-Z][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*"
)
_RESOLUTION_PROVENANCE = object()
_SCRUBBED_ENVIRONMENT = frozenset(
    {
        "ELAN_TOOLCHAIN",
        "LAKE",
        "LAKE_ARTIFACT_CACHE",
        "LAKE_CACHE_ARTIFACT_ENDPOINT",
        "LAKE_CACHE_DIR",
        "LAKE_CACHE_KEY",
        "LAKE_CACHE_REVISION_ENDPOINT",
        "LAKE_CACHE_SERVICE",
        "LAKE_CONFIG",
        "LAKE_HOME",
        "LAKE_NO_CACHE",
        "LAKE_PKG_URL_MAP",
        "LAKE_RESTORE_ARTIFACTS",
        "LEAN",
        "LEAN_AR",
        "LEAN_CC",
        "LEAN_GITHASH",
        "LEAN_PATH",
        "LEAN_SRC_PATH",
        "LEAN_SYSROOT",
        "PYTHONPATH",
    }
)


class LeanImportError(ValueError):
    """Structured imports cannot be resolved safely for the project."""


class LeanImportHeaderError(LeanImportError):
    """A source import header is unsafe to split from the request body."""


class StaleResolvedImportsError(LeanImportError):
    """A resolved import descriptor no longer identifies the same project."""


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    path: Path
    root: Path
    metadata: tuple[int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class _ModuleSelection:
    module: str
    artifact: _FileIdentity
    source: _FileIdentity | None


@dataclass(frozen=True, slots=True)
class _LakeRoots:
    bindings: tuple[tuple[Path, Path | None], ...]
    resolved: tuple[Path, ...]


@dataclass(frozen=True, slots=True, init=False)
class ResolvedImports:
    """Validated imports bound to one canonical Lake project root."""

    project_root: Path
    modules: tuple[str, ...]
    project_fingerprint: ProjectFingerprint
    _selections: tuple[_ModuleSelection, ...]
    _artifact_roots: _LakeRoots
    _source_roots: _LakeRoots
    _provenance: object = field(repr=False, compare=False)

    def __new__(cls, *args: object, **kwargs: object) -> ResolvedImports:
        raise TypeError("ResolvedImports values must come from project import resolution")

    def _assert_complete_resolution(self) -> None:
        if self._provenance is not _RESOLUTION_PROVENANCE:
            raise LeanImportError("untrusted resolved-import descriptor")
        if validate_imports(list(self.modules)) != self.modules:
            raise LeanImportError("resolved imports contain invalid module names")
        unique_modules = tuple(dict.fromkeys(self.modules))
        if not self.modules:
            if (
                self._selections
                or self._artifact_roots.bindings
                or self._source_roots.bindings
            ):
                raise LeanImportError("empty resolved imports contain discovery state")
            return
        if (
            not self._artifact_roots.bindings
            or not self._source_roots.bindings
            or not self._artifact_roots.resolved
            or not self._source_roots.resolved
            or any(
                resolved is not None
                and resolved not in self._artifact_roots.resolved
                for _, resolved in self._artifact_roots.bindings
            )
            or any(
                resolved is not None
                and resolved not in self._source_roots.resolved
                for _, resolved in self._source_roots.bindings
            )
            or tuple(selection.module for selection in self._selections)
            != unique_modules
            or any(
                selection.artifact.root not in self._artifact_roots.resolved
                or (
                    selection.source is not None
                    and selection.source.root not in self._source_roots.resolved
                )
                for selection in self._selections
            )
        ):
            raise LeanImportError("resolved imports contain incomplete discovery state")

    def assert_current(self, deadline: float) -> None:
        """Reject a descriptor after its canonical root or config changes."""
        try:
            self._assert_complete_resolution()
            _remaining(deadline)
            current_root = self.project_root.resolve(strict=True)
            if current_root != self.project_root or not current_root.is_dir():
                raise LeanImportError("the canonical project root changed")
            current = lean_project_fingerprint(current_root)
            _remaining(deadline)
            if self.modules:
                _require_lake_roots_current(self._artifact_roots, deadline)
                _require_lake_roots_current(self._source_roots, deadline)
                current_selections = _resolve_module_selections(
                    tuple(dict.fromkeys(self.modules)),
                    self._artifact_roots.resolved,
                    self._source_roots.resolved,
                    deadline,
                )
                if current_selections != self._selections:
                    raise StaleResolvedImportsError(
                        "resolved Lean imports are stale: import artifacts changed"
                    )
        except (TimeoutError, StaleResolvedImportsError):
            raise
        except (LeanImportError, OSError, RuntimeError) as error:
            raise StaleResolvedImportsError(
                f"resolved Lean imports are stale: {error}"
            ) from error
        if current != self.project_fingerprint:
            raise StaleResolvedImportsError(
                "resolved Lean imports are stale: project configuration changed"
            )


def clean_lake_environment() -> dict[str, str]:
    """Return the host environment without ambient Lean/Lake path overrides."""
    environment = os.environ.copy()
    for name in _SCRUBBED_ENVIRONMENT:
        environment.pop(name, None)
    return environment


def validate_imports(value: Any) -> tuple[str, ...] | None:
    """Validate an optional JSON import list while preserving its exact order."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise LeanImportError("imports must be an array of Lean module names or null")
    modules: list[str] = []
    for index, module in enumerate(value):
        if not isinstance(module, str) or not module:
            raise LeanImportError(f"imports[{index}] must be a non-empty Lean module name")
        parts = module.split(".")
        if any(_MODULE_PART.fullmatch(part) is None for part in parts):
            raise LeanImportError(f"imports[{index}] is not a conservative Lean module name: {module!r}")
        modules.append(module)
    return tuple(modules)


def split_imports_and_body(code: str) -> tuple[list[str], str, int]:
    """Split the conservative source-header subset accepted by Autoform."""
    lines = code.splitlines(keepends=True)
    imports: list[str] = []
    offset = 0
    body_start = 0
    header_line_count = 0
    block_depth = 0
    bare_carriage_return_in_prefix = False
    for index, line in enumerate(lines):
        bare_carriage_return = line.endswith("\r") and not line.endswith("\r\n")
        content = line[:-1] if line.endswith("\n") else line
        visible, next_depth = _mask_header_comments(content, block_depth)
        match = re.fullmatch(
            rf" *import +({_SOURCE_HEADER_MODULE.pattern}) *\r?",
            visible,
        )
        header_space = _only_header_space(visible)
        looks_like_header = _looks_like_import_header(visible)
        if match is not None and (
            bare_carriage_return or bare_carriage_return_in_prefix
        ):
            raise LeanImportHeaderError(
                "unsupported Lean source import header; pass module names with imports"
            )
        if match is not None:
            imports.append(match.group(1))
            body_start = offset + len(line)
            header_line_count = index + 1
            block_depth = next_depth
            # Removing only the first line of a multiline trailing comment
            # would expose its closing token to Lean. Preserve the whole source
            # and let Lean reject or diagnose it from the established base env.
            if block_depth:
                return imports, code, 0
        elif header_space:
            bare_carriage_return_in_prefix |= bare_carriage_return
            block_depth = next_depth
        elif looks_like_header:
            raise LeanImportHeaderError(
                "unsupported Lean source import header; pass module names with imports"
            )
        else:
            break
        offset += len(line)
    if block_depth:
        return imports, code, 0
    if not imports:
        return [], code, 0
    return imports, code[body_start:], header_line_count


def _only_header_space(value: str) -> bool:
    return all(character in {" ", "\r"} for character in value)


def _looks_like_import_header(value: str) -> bool:
    candidate = value.lstrip()
    if not candidate:
        return False
    tokens = candidate.split()
    first = tokens[0]
    if first in {"import", "module", "prelude"}:
        return True
    return first in {"public", "meta"} and "import" in tokens[1:3]


def _mask_header_comments(line: str, initial_depth: int) -> tuple[str, int]:
    """Mask ordinary Lean comments without joining surrounding tokens."""
    visible = list(line)
    depth = initial_depth
    index = 0
    while index < len(line):
        if depth and line.startswith("/-", index):
            visible[index : index + 2] = "  "
            depth += 1
            index += 2
        elif depth and line.startswith("-/", index):
            visible[index : index + 2] = "  "
            depth -= 1
            index += 2
        elif depth:
            visible[index] = " "
            index += 1
        elif line.startswith("--", index):
            visible[index:] = " " * (len(line) - index)
            break
        elif line.startswith("/-", index) and not line.startswith(("/--", "/-!"), index):
            visible[index : index + 2] = "  "
            depth = 1
            index += 2
        else:
            index += 1
    return "".join(visible), depth


def resolve_project_imports(
    project_root: Path,
    modules: tuple[str, ...],
    *,
    timeout: float | None = None,
    deadline: float | None = None,
) -> ResolvedImports:
    """Require fresh, unambiguous OLean artifacts in Lake-derived roots."""
    def resolved(
        *,
        canonical_root: Path,
        validated_modules: tuple[str, ...],
        fingerprint: ProjectFingerprint,
        selections: tuple[_ModuleSelection, ...] = (),
        artifact_roots: _LakeRoots | None = None,
        source_roots: _LakeRoots | None = None,
    ) -> ResolvedImports:
        artifact_roots = artifact_roots or _LakeRoots((), ())
        source_roots = source_roots or _LakeRoots((), ())
        result = object.__new__(ResolvedImports)
        object.__setattr__(result, "project_root", canonical_root)
        object.__setattr__(result, "modules", validated_modules)
        object.__setattr__(result, "project_fingerprint", fingerprint)
        object.__setattr__(result, "_selections", selections)
        object.__setattr__(result, "_artifact_roots", artifact_roots)
        object.__setattr__(result, "_source_roots", source_roots)
        object.__setattr__(result, "_provenance", _RESOLUTION_PROVENANCE)
        result._assert_complete_resolution()
        return result

    if deadline is None:
        if timeout is None:
            raise TypeError("resolve_project_imports requires timeout or deadline")
        deadline = time.monotonic() + timeout
    elif timeout is not None:
        raise TypeError("pass timeout or deadline, not both")
    _remaining(deadline)
    validated_modules = validate_imports(list(modules))
    assert validated_modules is not None
    modules = validated_modules
    try:
        project_root = project_root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise LeanImportError(f"invalid Lean project root: {project_root}") from error
    if not project_root.is_dir():
        raise LeanImportError(f"Lean project root is not a directory: {project_root}")
    try:
        fingerprint = lean_project_fingerprint(project_root)
    except OSError as error:
        raise LeanImportError("cannot fingerprint the Lean project") from error
    _remaining(deadline)
    if not modules:
        return resolved(
            canonical_root=project_root,
            validated_modules=modules,
            fingerprint=fingerprint,
        )
    manifest = project_root / "lake-manifest.json"
    _require_regular_manifest(manifest, deadline)
    environment = _lake_environment(project_root, deadline=deadline)
    _require_regular_manifest(manifest, deadline)
    _require_project_fingerprint(project_root, fingerprint, deadline)

    artifact_roots = _environment_roots(environment, "LEAN_PATH", deadline)
    source_roots = _environment_roots(environment, "LEAN_SRC_PATH", deadline)
    unique_modules = tuple(dict.fromkeys(modules))
    selections = _resolve_module_selections(
        unique_modules,
        artifact_roots.resolved,
        source_roots.resolved,
        deadline,
    )
    result = _run_lake(
        [
            "lake",
            "--rehash",
            "--no-build",
            "build",
            *(f"+{module}:olean" for module in unique_modules),
        ],
        project_root,
        deadline=deadline,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise LeanImportError(
            "requested Lean modules or their dependencies are stale; "
            f"run `lake build` first ({detail or 'Lake reported an out-of-date target'})"
        )
    _remaining(deadline)
    _require_regular_manifest(manifest, deadline)
    _require_project_fingerprint(project_root, fingerprint, deadline)
    if (
        _resolve_module_selections(
            unique_modules,
            artifact_roots.resolved,
            source_roots.resolved,
            deadline,
        )
        != selections
    ):
        raise LeanImportError(
            "Lean import artifacts changed during import discovery; retry the request"
        )
    return resolved(
        canonical_root=project_root,
        validated_modules=modules,
        fingerprint=fingerprint,
        selections=selections,
        artifact_roots=artifact_roots,
        source_roots=source_roots,
    )


def _require_project_fingerprint(
    project_root: Path,
    expected: ProjectFingerprint,
    deadline: float,
) -> None:
    _remaining(deadline)
    try:
        current = lean_project_fingerprint(project_root)
    except OSError as error:
        raise LeanImportError(
            "Lean project changed during import discovery; retry the request"
        ) from error
    _remaining(deadline)
    if current != expected:
        raise LeanImportError(
            "Lean project changed during import discovery; retry the request"
        )


def _require_regular_manifest(path: Path, deadline: float) -> None:
    """Validate the manifest without copying attacker-sized contents."""
    _remaining(deadline)
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise LeanImportError(
            "project imports require lake-manifest.json; run `lake update` and `lake build` first"
        ) from error
    except OSError as error:
        raise LeanImportError("cannot inspect lake-manifest.json") from error
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise LeanImportError("lake-manifest.json must be a regular file, not a symlink")
    if metadata.st_size > _MAX_MANIFEST_BYTES:
        raise LeanImportError("lake-manifest.json exceeded the size limit")
    _remaining(deadline)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("timed out discovering project imports")
    return remaining


def _lake_environment(project_root: Path, *, deadline: float) -> dict[str, str]:
    result = _run_lake(
        ["lake", "--no-build", "env"],
        project_root,
        deadline=deadline,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise LeanImportError(f"Lake import discovery failed: {detail or 'unknown error'}")
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeError as error:
        raise LeanImportError("Lake import discovery returned non-UTF-8 output") from error
    environment: dict[str, str] = {}
    for line in text.splitlines():
        name, separator, value = line.partition("=")
        if not separator or not name or name in environment:
            raise LeanImportError("Lake import discovery returned malformed environment data")
        environment[name] = value
    return environment


def _run_lake(
    command: list[str],
    project_root: Path,
    *,
    timeout: float | None = None,
    deadline: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
    if deadline is not None and timeout is not None:
        raise TypeError("pass timeout or deadline, not both")
    if deadline is None:
        if timeout is None:
            raise TypeError("_run_lake requires timeout or deadline")
        if timeout <= 0:
            raise TimeoutError("no request time remains for Lake import discovery")
        deadline = time.monotonic() + timeout
    elif deadline - time.monotonic() <= 0:
        raise TimeoutError("no request time remains for Lake import discovery")
    try:
        process = subprocess.Popen(
            command,
            cwd=project_root,
            env=clean_lake_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise LeanImportError(f"cannot run Lake import discovery: {error}") from error
    assert process.stdout is not None and process.stderr is not None
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    total_bytes = 0
    selector = selectors.DefaultSelector()
    completed = False
    try:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out discovering project imports")
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fileobj.fileno(), _READ_CHUNK_BYTES)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total_bytes += len(chunk)
                if total_bytes > _MAX_DISCOVERY_OUTPUT_BYTES:
                    raise LeanImportError("Lake import discovery output exceeded the size limit")
                streams[key.fileobj].extend(chunk)
        returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        completed = True
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("timed out discovering project imports") from error
    finally:
        selector.close()
        if not completed:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                cleanup_budget = min(
                    _PROCESS_KILL_WAIT_SECONDS,
                    max(0.0, deadline - time.monotonic()),
                )
                try:
                    process.wait(timeout=cleanup_budget)
                except subprocess.TimeoutExpired:
                    pass
        process.stdout.close()
        process.stderr.close()
    return subprocess.CompletedProcess(
        command,
        returncode,
        bytes(streams[process.stdout]),
        bytes(streams[process.stderr]),
    )


def _environment_roots(
    environment: dict[str, str], name: str, deadline: float
) -> _LakeRoots:
    value = environment.get(name)
    if value is None:
        raise LeanImportError(f"Lake import discovery did not provide {name}")
    bindings: list[tuple[Path, Path | None]] = []
    roots: list[Path] = []
    for raw in value.split(os.pathsep):
        _remaining(deadline)
        if not raw:
            raise LeanImportError(f"Lake import discovery returned an empty {name} entry")
        path = Path(raw)
        if not path.is_absolute():
            raise LeanImportError(f"Lake import discovery returned a relative {name} entry")
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            bindings.append((path, None))
            continue
        except (OSError, RuntimeError) as error:
            raise LeanImportError(
                f"cannot inspect Lake import root: {path}"
            ) from error
        if not resolved.is_dir():
            raise LeanImportError(f"Lake import root is not a directory: {resolved}")
        bindings.append((path, resolved))
        if resolved not in roots:
            roots.append(resolved)
    if not roots:
        raise LeanImportError(f"Lake import discovery provided no existing {name} roots")
    return _LakeRoots(tuple(bindings), tuple(roots))


def _require_lake_roots_current(roots: _LakeRoots, deadline: float) -> None:
    for raw, expected in roots.bindings:
        _remaining(deadline)
        try:
            current = raw.resolve(strict=True)
        except FileNotFoundError as error:
            if expected is None:
                continue
            raise LeanImportError(f"Lake import root changed: {raw}") from error
        except (OSError, RuntimeError) as error:
            raise LeanImportError(f"Lake import root changed: {raw}") from error
        if expected is None or current != expected or not current.is_dir():
            raise LeanImportError(f"Lake import root changed: {raw}")


def _resolve_module_selections(
    modules: tuple[str, ...],
    artifact_roots: tuple[Path, ...],
    source_roots: tuple[Path, ...],
    deadline: float,
) -> tuple[_ModuleSelection, ...]:
    selections: list[_ModuleSelection] = []
    for module in modules:
        _remaining(deadline)
        relative = Path(*module.split("."))
        artifacts = _matching_files(
            artifact_roots,
            relative.with_suffix(".olean"),
            deadline,
        )
        if not artifacts:
            raise LeanImportError(
                f"Lean module {module!r} is not built for this project; "
                "run `lake build` first"
            )
        if len(artifacts) != 1:
            rendered = ", ".join(str(item.path) for item in artifacts)
            raise LeanImportError(
                f"Lean module {module!r} is ambiguous across Lake roots: {rendered}"
            )
        sources = _matching_files(
            source_roots,
            relative.with_suffix(".lean"),
            deadline,
        )
        if len(sources) > 1:
            rendered = ", ".join(str(item.path) for item in sources)
            raise LeanImportError(
                f"Lean module {module!r} has ambiguous sources: {rendered}"
            )
        selections.append(
            _ModuleSelection(
                module=module,
                artifact=artifacts[0],
                source=sources[0] if sources else None,
            )
        )
    return tuple(selections)


def _matching_files(
    roots: tuple[Path, ...], relative: Path, deadline: float
) -> tuple[_FileIdentity, ...]:
    matches: list[_FileIdentity] = []
    for root in roots:
        _remaining(deadline)
        candidate = root / relative
        if not candidate.exists() and not candidate.is_symlink():
            continue
        matches.append(_require_regular_contained(candidate, root, deadline))
    return tuple(matches)


def _require_regular_contained(
    path: Path, root: Path, deadline: float
) -> _FileIdentity:
    _remaining(deadline)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        metadata = path.lstat()
    except (OSError, RuntimeError, ValueError) as error:
        raise LeanImportError(f"Lean import artifact escapes its Lake root: {path}") from error
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise LeanImportError(f"Lean import artifact is not a regular file: {path}")
    _remaining(deadline)
    return _FileIdentity(
        path=resolved,
        root=root,
        metadata=(
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        ),
    )
