"""Export a blueprint dependency graph as a Mermaid Markdown page."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import os
import secrets
import stat
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

from . import mermaid, status
from ._tree_snapshot import (
    TreeSelection,
    TreeSnapshot,
    TreeSnapshotError,
    capture_directory_descriptor,
)
from .graph import Graph, GraphValidationError, load_graph
from .runtime import (
    RuntimePaths,
    RuntimeProjectionError,
    bind_runtime_paths,
)
from .workspace import _open_workspace_root
from .workspace_manifest import WorkspaceError


GENERATED_STRUCTURE_MARKER = "---\nkind: structure\nautoform_generated: true\n---"


class VisualizationError(ValueError):
    """Raised when visualization output would overwrite authored content."""


@dataclass(frozen=True, slots=True)
class _OutputExpectation:
    parent_identity: tuple[int, int]
    file_state: _EntryState | None


@dataclass(frozen=True, slots=True)
class _CapturedVisualization:
    snapshot: TreeSnapshot
    selection: TreeSelection
    graph: Graph
    revision: str


@dataclass(frozen=True, slots=True)
class _EntryState:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    payload_sha256: bytes | None


@dataclass(frozen=True, slots=True)
class _AtomicWriteState:
    destination: str
    temporary: str
    staged_identity: _EntryState
    original_identity: _EntryState | None


@dataclass(slots=True)
class _DeferredOutput:
    directory_descriptor: int
    state: _AtomicWriteState
    verify_parent: Callable[[], None]
    close_parent: Callable[[], None]
    closed: bool = False

    def rollback(self) -> None:
        try:
            _rollback_atomic_write(
                self.directory_descriptor,
                self.state.destination,
                self.state.temporary,
                staged_identity=self.state.staged_identity,
                original_identity=self.state.original_identity,
                require_original=True,
            )
            if _entry_state_at(self.directory_descriptor, self.state.temporary) != (
                self.state.staged_identity
            ):
                raise VisualizationError("staged generated output changed after rollback")
            os.unlink(self.state.temporary, dir_fd=self.directory_descriptor)
            os.fsync(self.directory_descriptor)
            self.verify_parent()
        finally:
            self.close()

    def verify(self) -> None:
        self.verify_parent()
        _verify_atomic_commit(
            self.directory_descriptor,
            self.state.destination,
            self.state.temporary,
            staged_identity=self.state.staged_identity,
            original_identity=self.state.original_identity,
        )

    def finalize(self) -> None:
        try:
            self.verify()
            if self.state.original_identity is not None:
                try:
                    os.unlink(self.state.temporary, dir_fd=self.directory_descriptor)
                except FileNotFoundError:
                    raise VisualizationError(
                        "displaced generated output disappeared before finalization"
                    ) from None
                os.fsync(self.directory_descriptor)
        finally:
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_parent()


def _destination(path: Path) -> Path:
    """Canonicalize the parent without following the final destination symlink."""
    path = path.absolute()
    return path.parent.resolve() / path.name


def _replace(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _rename_implementation(*, exchange: bool):
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError as error:
        raise VisualizationError(
            "atomic visualization publication is unavailable on this platform"
        ) from error
    if hasattr(libc, "renameatx_np"):
        function = libc.renameatx_np
        flag = 0x00000002 if exchange else 0x00000004
    elif hasattr(libc, "renameat2"):
        function = libc.renameat2
        flag = 2 if exchange else 1
    else:
        raise VisualizationError(
            "atomic visualization publication is unavailable on this platform"
        )
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    return function, flag


def _rename_at(
    source: str,
    destination: str,
    directory_descriptor: int,
    *,
    exchange: bool,
) -> None:
    function, flag = _rename_implementation(exchange=exchange)
    result = function(
        directory_descriptor,
        os.fsencode(source),
        directory_descriptor,
        os.fsencode(destination),
        flag,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _replace_at(source: str, destination: str, directory_descriptor: int) -> bool:
    """Exchange with an existing name, or install without replacing a new one."""

    try:
        _rename_at(source, destination, directory_descriptor, exchange=True)
    except OSError as error:
        if error.errno != errno.ENOENT:
            raise
        _rename_at(source, destination, directory_descriptor, exchange=False)
        return False
    return True


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _atomic_write(destination: Path, contents: str) -> None:
    """Atomically replace *destination* from a temporary file beside it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        _replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_at(
    directory_descriptor: int,
    destination: str,
    contents: str,
    *,
    verify_parent: Callable[[], None],
    expected_file: _EntryState | None | bool = False,
    source_guard: Callable[[], None] | None = None,
    defer_finalize: bool = False,
) -> _AtomicWriteState | None:
    """Atomically replace one file beneath an already retained directory."""

    if Path(destination).name != destination or destination in {"", ".", ".."}:
        raise VisualizationError("generated output must be a direct blueprint child")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    temporary = f".{destination}.{secrets.token_hex(16)}.tmp"
    descriptor: int | None = None
    preserve_temporary = False
    commit_attempted = False
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_descriptor)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            stream.write(contents)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        staged_identity = _entry_state_at(directory_descriptor, temporary)
        assert staged_identity is not None
        verify_parent()
        if source_guard is not None:
            source_guard()
        original_identity = (
            _entry_state_at(directory_descriptor, destination)
            if expected_file is False
            else _verify_output_expectation(
                directory_descriptor,
                destination,
                expected_file,
            )
        )
        if original_identity is not None and not stat.S_ISREG(original_identity.mode):
            raise VisualizationError("generated output must be a regular file")
        try:
            commit_attempted = True
            _replace_at(temporary, destination, directory_descriptor)
            preserve_temporary = True
            _verify_atomic_commit(
                directory_descriptor,
                destination,
                temporary,
                staged_identity=staged_identity,
                original_identity=original_identity,
            )
            os.fsync(directory_descriptor)
            verify_parent()
            if source_guard is not None:
                source_guard()
        except BaseException as error:
            if commit_attempted:
                try:
                    _rollback_atomic_write(
                        directory_descriptor,
                        destination,
                        temporary,
                        staged_identity=staged_identity,
                        original_identity=original_identity,
                        require_original=False,
                    )
                except BaseException:
                    preserve_temporary = True
                    raise VisualizationError(
                        "generated output changed during publication; recovery was retained"
                    ) from error
                preserve_temporary = False
            raise
        if defer_finalize:
            preserve_temporary = True
            return _AtomicWriteState(
                destination=destination,
                temporary=temporary,
                staged_identity=staged_identity,
                original_identity=original_identity,
            )
        if original_identity is not None:
            os.unlink(temporary, dir_fd=directory_descriptor)
            preserve_temporary = False
            os.fsync(directory_descriptor)
        return None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if not preserve_temporary:
            try:
                os.unlink(temporary, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass


def _rollback_atomic_write(
    directory_descriptor: int,
    destination: str,
    temporary: str,
    *,
    staged_identity: _EntryState,
    original_identity: _EntryState | None,
    require_original: bool,
) -> None:
    """Undo one generated-file replacement without overwriting a third party."""

    current = _entry_state_at(directory_descriptor, destination)
    displaced = _entry_state_at(directory_descriptor, temporary)
    if current == staged_identity:
        if require_original and displaced != original_identity:
            raise VisualizationError("displaced generated output changed before rollback")
        recovery_identity = displaced
        if displaced is None:
            _rename_at(destination, temporary, directory_descriptor, exchange=False)
        else:
            _rename_at(temporary, destination, directory_descriptor, exchange=True)
        restored = _entry_state_at(directory_descriptor, destination)
        retained = _entry_state_at(directory_descriptor, temporary)
        if restored != recovery_identity or retained != staged_identity:
            raise VisualizationError("generated output changed during rollback")
    elif displaced != staged_identity:
        raise VisualizationError("generated output changed before rollback")
    os.fsync(directory_descriptor)


def _entry_state_at(
    directory_descriptor: int,
    name: str,
) -> _EntryState | None:
    try:
        before = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    payload_sha256: bytes | None = None
    if stat.S_ISREG(before.st_mode):
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
        try:
            opened = os.fstat(descriptor)
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        named = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if (
            _file_identity(before) != _file_identity(opened)
            or _file_identity(opened) != _file_identity(after)
            or _file_identity(after) != _file_identity(named)
        ):
            raise VisualizationError("generated output changed while it was inspected")
        payload_sha256 = digest.digest()
    elif stat.S_ISLNK(before.st_mode):
        target = os.readlink(name, dir_fd=directory_descriptor)
        after = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if _file_identity(before) != _file_identity(after):
            raise VisualizationError("generated output changed while it was inspected")
        payload_sha256 = hashlib.sha256(os.fsencode(target)).digest()
    else:
        after = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if _file_identity(before) != _file_identity(after):
            raise VisualizationError("generated output changed while it was inspected")
    return _EntryState(
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        payload_sha256,
    )


def _verify_atomic_commit(
    directory_descriptor: int,
    destination: str,
    temporary: str,
    *,
    staged_identity: _EntryState,
    original_identity: _EntryState | None,
) -> None:
    if _entry_state_at(directory_descriptor, destination) != staged_identity:
        raise VisualizationError("generated output changed during publication")
    if _entry_state_at(directory_descriptor, temporary) != original_identity:
        raise VisualizationError(
            "the displaced generated output changed during the commit operation"
        )


def _verify_output_expectation(
    directory_descriptor: int,
    destination: str,
    expected: _EntryState | None,
) -> _EntryState | None:
    current = _entry_state_at(directory_descriptor, destination)
    if current != expected:
        raise VisualizationError("generated output changed after ownership preflight")
    return current


def _preflight_structure(destination: Path) -> None:
    if not destination.exists() and not destination.is_symlink():
        return
    try:
        existing = destination.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise VisualizationError(
            f"refusing to overwrite existing structure page without the generated marker: {destination}"
        ) from error
    if not existing.startswith(f"{GENERATED_STRUCTURE_MARKER}\n"):
        raise VisualizationError(
            f"refusing to overwrite authored structure page without the generated marker: {destination}"
        )


def _preflight_structure_at(
    directory_descriptor: int,
    destination: str,
    display_path: Path,
) -> _EntryState | None:
    """Inspect a generated page through the retained blueprint descriptor."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor: int | None = None
    try:
        try:
            expected = os.stat(destination, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(expected.st_mode):
            raise VisualizationError(
                "refusing to overwrite existing structure page without the generated marker: "
                f"{display_path}"
            )
        descriptor = os.open(destination, flags, dir_fd=directory_descriptor)
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        named = os.stat(destination, dir_fd=directory_descriptor, follow_symlinks=False)
        if (
            _file_identity(expected) != _file_identity(opened)
            or _file_identity(opened) != _file_identity(after)
            or _file_identity(after) != _file_identity(named)
        ):
            raise OSError("structure page changed")
        try:
            payload = b"".join(chunks)
            existing = payload.decode("utf-8")
        except UnicodeError as error:
            raise VisualizationError(
                "refusing to overwrite existing structure page without the generated marker: "
                f"{display_path}"
            ) from error
    except OSError as error:
        raise VisualizationError(
            "refusing to overwrite existing structure page without the generated marker: "
            f"{display_path}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not existing.startswith(f"{GENERATED_STRUCTURE_MARKER}\n"):
        raise VisualizationError(
            f"refusing to overwrite authored structure page without the generated marker: {display_path}"
        )
    return _EntryState(
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        hashlib.sha256(payload).digest(),
    )


def _write_output(
    paths: RuntimePaths,
    destination: Path,
    contents: str,
    *,
    expected: _OutputExpectation | None = None,
    source_guard: Callable[[], None] | None = None,
    defer_finalize: bool = False,
) -> _DeferredOutput | None:
    if destination.parent == paths.blueprint_dir:
        descriptor = paths.duplicate_blueprint_descriptor()
        transferred = False
        try:
            if expected is not None and expected.parent_identity != paths.blueprint_identity:
                raise VisualizationError("structure output parent changed after ownership preflight")
            state = _atomic_write_at(
                descriptor,
                destination.name,
                contents,
                verify_parent=paths.verify,
                expected_file=expected.file_state if expected is not None else False,
                source_guard=source_guard,
                defer_finalize=defer_finalize,
            )
            if state is not None:
                transferred = True
                return _DeferredOutput(
                    descriptor,
                    state,
                    paths.verify,
                    lambda: os.close(descriptor),
                )
        finally:
            if not transferred:
                os.close(descriptor)
        paths.verify()
        return None
    paths.verify()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        parent = _open_workspace_root(destination.parent)
    except WorkspaceError as error:
        raise VisualizationError("output directory cannot be retained safely") from error
    try:
        if expected is not None and expected.parent_identity != parent.identity:
            raise VisualizationError("structure output parent changed after ownership preflight")

        def verify_external_parent() -> None:
            try:
                parent.verify()
            except OSError:
                raise VisualizationError("output directory changed during publication") from None

        state = _atomic_write_at(
            parent.descriptor,
            destination.name,
            contents,
            verify_parent=verify_external_parent,
            expected_file=expected.file_state if expected is not None else False,
            source_guard=source_guard,
            defer_finalize=defer_finalize,
        )
        if state is not None:
            retained_parent = parent
            parent = None
            return _DeferredOutput(
                retained_parent.descriptor,
                state,
                verify_external_parent,
                retained_parent.close,
            )
        paths.verify()
        return None
    finally:
        if parent is not None:
            parent.close()


def _write_outputs_transactionally(
    paths: RuntimePaths,
    outputs: Iterable[tuple[Path, str, _OutputExpectation | None]],
    *,
    source_guard: Callable[[], None],
) -> None:
    """Publish related generated files together or restore every prior file."""

    committed: list[_DeferredOutput] = []
    try:
        for destination, contents, expected in outputs:
            result = _write_output(
                paths,
                destination,
                contents,
                expected=expected,
                source_guard=source_guard,
                defer_finalize=True,
            )
            assert result is not None
            committed.append(result)
        for result in committed:
            result.verify()
        source_guard()
        paths.verify()
        for result in committed:
            result.verify()
    except BaseException:
        rollback_error: BaseException | None = None
        for result in reversed(committed):
            try:
                result.rollback()
            except BaseException as current:
                if rollback_error is None:
                    rollback_error = current
        if rollback_error is not None:
            raise VisualizationError(
                "generated outputs changed during publication; recovery could not be completed: "
                f"{rollback_error}"
            ) from rollback_error
        raise
    for index, result in enumerate(committed):
        try:
            result.finalize()
        except BaseException:
            for pending in committed[index + 1 :]:
                pending.close()
            raise


def _preflight_structure_output(
    paths: RuntimePaths,
    destination: Path,
) -> _OutputExpectation:
    if destination.parent == paths.blueprint_dir:
        descriptor = paths.duplicate_blueprint_descriptor()
        try:
            expected = _preflight_structure_at(descriptor, destination.name, destination)
        finally:
            os.close(descriptor)
        paths.verify()
        assert paths.blueprint_identity is not None
        return _OutputExpectation(paths.blueprint_identity, expected)
    paths.verify()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        parent = _open_workspace_root(destination.parent)
    except WorkspaceError as error:
        raise VisualizationError("output directory cannot be retained safely") from error
    try:
        expected = _preflight_structure_at(parent.descriptor, destination.name, destination)
        parent.verify()
        paths.verify()
        return _OutputExpectation(parent.identity, expected)
    finally:
        parent.close()


def _preflight_output(paths: RuntimePaths, destination: Path) -> _OutputExpectation:
    """Capture the exact output name generation before rendering begins."""

    if destination.parent == paths.blueprint_dir:
        descriptor = paths.duplicate_blueprint_descriptor()
        try:
            expected = _entry_state_at(descriptor, destination.name)
        finally:
            os.close(descriptor)
        paths.verify()
        assert paths.blueprint_identity is not None
        return _OutputExpectation(paths.blueprint_identity, expected)
    paths.verify()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        parent = _open_workspace_root(destination.parent)
    except WorkspaceError as error:
        raise VisualizationError("output directory cannot be retained safely") from error
    try:
        expected = _entry_state_at(parent.descriptor, destination.name)
        parent.verify()
        paths.verify()
        return _OutputExpectation(parent.identity, expected)
    finally:
        parent.close()


def _require_descriptor_bound_output(paths: RuntimePaths) -> None:
    """Fail before writing when the selected blueprint cannot be retained."""

    paths.require_strong_binding(operation="visualization output")


def export_graph(
    blueprint_dir: Path,
    output: Path | None = None,
    *,
    link_extension: str = ".md",
    title: str = "Dependency graph",
    _paths: RuntimePaths | None = None,
) -> Path:
    """Load and export ``blueprint_dir``; return the written Markdown path."""
    if _paths is None:
        with bind_runtime_paths(blueprint_dir) as paths:
            return export_graph(
                paths.blueprint_dir,
                output,
                link_extension=link_extension,
                title=title,
                _paths=paths,
            )
    blueprint_dir = _paths.blueprint_dir
    _require_descriptor_bound_output(_paths)
    destination = (
        blueprint_dir / "dependencies.md"
        if output is None
        else _destination(output)
    )
    expected = _preflight_output(_paths, destination)
    captured = _capture_visualization(_paths, (destination,))
    page = _render_captured_graph_page(
        captured.graph,
        _paths,
        destination,
        link_extension=link_extension,
        title=title,
    )
    _write_output(
        _paths,
        destination,
        page,
        expected=expected,
        source_guard=lambda: _require_visualization_generation(_paths, captured),
    )
    return destination


def _visualization_selection(
    paths: RuntimePaths,
    outputs: Iterable[Path],
) -> TreeSelection:
    excluded: set[PurePosixPath] = set()
    for output in outputs:
        try:
            relative = output.relative_to(paths.blueprint_dir)
        except ValueError:
            continue
        excluded.add(PurePosixPath(relative.as_posix()))

    def visible(relative: PurePosixPath) -> bool:
        return not any(part.startswith(".") for part in relative.parts)

    return TreeSelection(
        include=lambda relative, mode: (
            visible(relative)
            and relative not in excluded
            and (
                relative.suffix == ".md"
                or (
                    relative.parts[:1] == ("roadmap",)
                    and not stat.S_ISREG(mode)
                )
            )
        ),
        descend=visible,
    )


def _visualization_revision(snapshot: TreeSnapshot) -> str:
    included = {
        relative
        for relative, _value in (
            *snapshot.files,
            *snapshot.symlinks,
            *snapshot.special,
        )
    }
    relevant = TreeSnapshot(
        root_identity=(0, 0),
        directories=(),
        files=snapshot.files,
        symlinks=snapshot.symlinks,
        special=snapshot.special,
        placeholders=snapshot.placeholders,
        omitted=(),
        identities=tuple(
            (relative, identity)
            for relative, identity in snapshot.identities
            if relative in included
        ),
    )
    return relevant.generation_revision


def _capture_visualization(
    paths: RuntimePaths,
    outputs: Iterable[Path],
) -> _CapturedVisualization:
    selection = _visualization_selection(paths, outputs)
    descriptor = paths.duplicate_blueprint_descriptor()
    try:
        snapshot = capture_directory_descriptor(
            descriptor,
            expected_identity=paths.blueprint_identity,
            selection=selection,
        )
    except TreeSnapshotError as error:
        raise VisualizationError(f"could not capture blueprint: {error}") from error
    finally:
        os.close(descriptor)
    paths.verify()
    try:
        with tempfile.TemporaryDirectory(prefix="autoform-visualization-") as temporary:
            snapshot_root = Path(temporary) / "blueprint"
            snapshot.materialize(snapshot_root)
            graph = load_graph(snapshot_root)
    except TreeSnapshotError as error:
        raise VisualizationError(f"could not materialize blueprint: {error}") from error
    return _CapturedVisualization(
        snapshot=snapshot,
        selection=selection,
        graph=graph,
        revision=_visualization_revision(snapshot),
    )


def _require_visualization_generation(
    paths: RuntimePaths,
    captured: _CapturedVisualization,
) -> None:
    descriptor = paths.duplicate_blueprint_descriptor()
    try:
        current = capture_directory_descriptor(
            descriptor,
            expected_identity=paths.blueprint_identity,
            selection=captured.selection,
        )
    except TreeSnapshotError as error:
        raise VisualizationError("blueprint changed while visualization was prepared") from error
    finally:
        os.close(descriptor)
    paths.verify()
    if _visualization_revision(current) != captured.revision:
        raise VisualizationError("blueprint changed while visualization was prepared")


def _prepare_structure_page(
    paths: RuntimePaths,
    destination: Path,
    *,
    graph_output: Path | None = None,
) -> tuple[str, Graph, Callable[[], None]]:
    """Build a structure page without publishing any generated artifact."""

    _require_descriptor_bound_output(paths)
    outputs = [
        paths.blueprint_dir / "dependencies.md",
        paths.blueprint_dir / "structure.md",
        destination,
    ]
    if graph_output is not None:
        outputs.append(graph_output)
    captured = _capture_visualization(paths, outputs)
    snapshot = captured.snapshot
    graph = captured.graph
    statuses = status.derive(graph)
    by_path = {
        node.path.relative_to(graph.blueprint_dir): node
        for node in graph.nodes.values()
    }

    files = [
        Path(relative)
        for relative, _data in snapshot.files
        if Path(relative).suffix == ".md"
        and not any(part.startswith(".") for part in Path(relative).parts)
    ]
    files.sort()
    directories: set[Path] = set()
    for path in files:
        for parent in path.parents:
            if parent != Path("."):
                directories.add(parent)

    lines: list[str] = []
    for entry in sorted(directories | set(files)):
        indent = "    " * (len(entry.parts) - 1)
        if entry in directories:
            lines.append(f"{indent}- **{entry.name}/**")
            continue
        node = by_path.get(entry)
        if node is None:
            lines.append(f"{indent}- [{entry.name}]({entry.as_posix()}) · prose")
            continue
        kind = node.declaration or node.kind
        lines.append(
            f"{indent}- [{node.title}]({entry.as_posix()}) · {kind} · {statuses[node.id].label}"
        )

    depths = {len(path.parts) - 1 for path in by_path}
    warning = (
        "> [!warning] Every article sits directly under `roadmap/`.\n"
        "> Chapters come from directories, so this vault publishes as one\n"
        "> undivided list. Group the articles into subdirectories.\n\n"
        if len(by_path) > 3 and depths <= {1}
        else ""
    )
    paths.verify()
    return (
        (
            f"{GENERATED_STRUCTURE_MARKER}\n\n"
            "# Vault structure\n\n"
            "Every Markdown file in this vault, with the state the dependency graph\n"
            "derives for it. Chapters come from directories, so the shape of this\n"
            "tree is the shape of the published book.\n\n"
            f"{warning}"
            + "\n".join(lines)
            + "\n"
        ),
        graph,
        lambda: _require_visualization_generation(paths, captured),
    )


def _render_captured_graph_page(
    graph: Graph,
    paths: RuntimePaths,
    destination: Path,
    *,
    link_extension: str,
    title: str,
) -> str:
    """Render graph links against the live vault from captured node paths."""

    links = {
        node_id: mermaid.relative_link(
            paths.blueprint_dir / node.path.relative_to(graph.blueprint_dir),
            destination,
            link_extension,
        )
        for node_id, node in graph.nodes.items()
    }
    return mermaid.render_page(
        graph,
        status.derive(graph),
        destination,
        link_extension=link_extension,
        title=title,
        links=links,
    )


def export_structure(
    blueprint_dir: Path,
    output: Path | None = None,
    *,
    _paths: RuntimePaths | None = None,
) -> Path:
    """Write the vault's own structure page, for reading inside Obsidian.

    Obsidian's file explorer already shows the tree, so the part worth writing
    down is the part it cannot know: the derived state of each article, which
    comes from the dependency graph rather than from anything in the file. The
    flat-vault warning travels with it, because a vault with every article
    directly under ``roadmap/`` publishes a book with no chapters and looks
    perfectly ordinary in the explorer.

    Plain Markdown, no HTML: the site's stylesheet does not exist here.
    """
    if _paths is None:
        with bind_runtime_paths(blueprint_dir) as paths:
            return export_structure(paths.blueprint_dir, output, _paths=paths)
    blueprint_dir = _paths.blueprint_dir
    destination = blueprint_dir / "structure.md" if output is None else _destination(output)
    expected = _preflight_structure_output(_paths, destination)
    page, _graph, source_guard = _prepare_structure_page(_paths, destination)
    _write_output(
        _paths,
        destination,
        page,
        expected=expected,
        source_guard=source_guard,
    )
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "blueprint_dir",
        type=Path,
        help="workspace, legacy project, or directory containing roadmap Markdown nodes",
    )
    parser.add_argument("--project", help="registered workspace project id")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="output Markdown (default: <blueprint-dir>/dependencies.md)",
    )
    parser.add_argument(
        "--link-extension",
        choices=(".md", ".html"),
        default=".md",
        help="node-link extension: .md for the vault or .html for a built site",
    )
    parser.add_argument("--title", default="Dependency graph", help="page heading")
    parser.add_argument(
        "--structure",
        action="store_true",
        help="also generate <blueprint-dir>/structure.md when absent or previously generated",
    )
    args = parser.parse_args(argv)
    try:
        with bind_runtime_paths(args.blueprint_dir, project_id=args.project) as paths:
            blueprint_dir = paths.blueprint_dir
            structure = None
            structure_page = None
            structure_expectation = None
            graph_expectation = None
            captured_graph = None
            graph_output = (
                blueprint_dir / "dependencies.md"
                if args.output is None
                else _destination(args.output)
            )
            if args.structure:
                structure = blueprint_dir / "structure.md"
                if graph_output == structure:
                    raise VisualizationError(
                        f"graph and structure outputs must be different paths: {structure}"
                    )
                graph_expectation = _preflight_output(paths, graph_output)
                structure_expectation = _preflight_structure_output(paths, structure)
                structure_page, captured_graph, source_guard = _prepare_structure_page(
                    paths,
                    structure,
                    graph_output=graph_output,
                )
            if captured_graph is None:
                output = export_graph(
                    blueprint_dir,
                    args.output,
                    link_extension=args.link_extension,
                    title=args.title,
                    _paths=paths,
                )
            else:
                graph_page = _render_captured_graph_page(
                    captured_graph,
                    paths,
                    graph_output,
                    link_extension=args.link_extension,
                    title=args.title,
                )
                assert structure is not None
                assert structure_page is not None
                assert structure_expectation is not None
                assert graph_expectation is not None
                _write_outputs_transactionally(
                    paths,
                    (
                        (graph_output, graph_page, graph_expectation),
                        (structure, structure_page, structure_expectation),
                    ),
                    source_guard=source_guard,
                )
                output = graph_output
            paths.verify()
    except (GraphValidationError, RuntimeProjectionError, VisualizationError) as error:
        parser.exit(2, f"error: {error}\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
