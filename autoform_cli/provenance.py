"""Resolve and verify immutable provenance for the running Autoform plugin.

The source and revision emitted here are persisted in generated workflows.  A
candidate is therefore returned only when its remote commit is obtainable and
the installed runtime and plugin surface match that commit.
"""

from __future__ import annotations

import importlib.util
import json
import os
import py_compile
import re
import selectors
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

try:  # pragma: no cover - exercised only on Python 3.10
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


INSTALL_RECORD = ".codex-marketplace-install.json"
MAX_INSTALL_RECORD_BYTES = 64 * 1024

_MAX_GIT_TEXT_BYTES = 16 * 1024
_MAX_GIT_LIST_BYTES = 8 * 1024 * 1024
_MAX_MANIFEST_ENTRIES = 20_000
_MAX_SHIPPED_FILE_BYTES = 16 * 1024 * 1024
_MAX_SHIPPED_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_PATH_DEPTH = 64

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_SOURCE_HOST = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
)
_SOURCE_PATH_PART = re.compile(r"[A-Za-z0-9._~-]+")
_GITHUB_SCP_SOURCE = re.compile(
    r"git@github\.com:(?P<path>[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)+)"
)
_BYTECODE_NAME = re.compile(
    r"(?P<stem>.+?)\.(?P<tag>[A-Za-z0-9_-]+)"
    r"(?:\.opt-(?P<optimization>[A-Za-z0-9]+))?\.pyc"
)

# These paths are consumed by a plugin host or by the packaged Python runtime.
# Tests, repository policy, and CI files are development inputs, not installed
# executable state.  Package roots declared by pyproject.toml are added below.
_SHIPPED_ROOTS = frozenset(
    {
        ".claude-plugin",
        ".codex-plugin",
        ".muse-plugin",
        "assets",
        "skills",
    }
)
_OPTIONAL_SHIPPED_ROOTS = frozenset({"agents", "commands", "hooks"})
_SHIPPED_FILES = frozenset({".mcp.json", "pyproject.toml", "uv.lock"})

# These are host- or tool-owned state rather than source.  The exact list is
# deliberately local; arbitrary gitignored paths are not automatically trusted.
_DERIVED_ROOTS = frozenset(
    {
        ".claude",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "dist",
        "node_modules",
    }
)
_DERIVED_DIRECTORY_NAMES = frozenset({".lake", "__pycache__", "site", "site-src"})
_DERIVED_FILE_NAMES = frozenset({".DS_Store", ".zuliprc"})
_IMPORTABLE_SUFFIXES = frozenset({".py", ".pyc", ".pyo", ".pth", ".so", ".pyd", ".dylib"})
_PLUGIN_ROOT = Path(os.path.abspath(Path(__file__).parent.parent))


class ProvenanceError(ValueError):
    """The running plugin could not be tied to one verified remote commit."""

    code = "project-provenance-unavailable"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PluginProvenance:
    """A credential-free Git source and the exact verified commit it serves."""

    source: str
    revision: str

    def as_dict(self) -> dict[str, object]:
        return {"ok": True, "revision": self.revision, "source": self.source}


@dataclass(frozen=True, slots=True)
class _Candidate:
    source: str
    revision: str


@dataclass(frozen=True, slots=True)
class _TreeObject:
    mode: int
    kind: str
    object_id: str


@dataclass(frozen=True, slots=True)
class _ManifestEntry:
    mode: int
    content: bytes


@dataclass(frozen=True, slots=True)
class _SourceLayout:
    files: dict[str, _ManifestEntry]
    all_files: frozenset[str]
    roots: tuple[str, ...]
    package_roots: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ActualEntry:
    mode: int
    content: bytes
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _CachedBytecode:
    parent: str
    name: str
    content: bytes


class _GitFailure(RuntimeError):
    pass


class _InvalidJson(ValueError):
    pass


def plugin_root() -> Path:
    """Return the source root that contains the running ``autoform_cli``."""

    return _PLUGIN_ROOT


def normalize_git_source(
    source: str,
    *,
    allow_github_scp: bool = False,
    add_git_suffix: bool = False,
) -> str | None:
    """Return a canonical credential-free HTTPS Git URL, or ``None``."""

    if not isinstance(source, str) or not source or source != source.strip():
        return None
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in source):
        return None
    if allow_github_scp:
        scp = _GITHUB_SCP_SOURCE.fullmatch(source)
        if scp is not None:
            source = f"https://github.com/{scp.group('path')}"
    try:
        parsed = urlsplit(source)
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc.lower() != hostname.lower()
        or _SOURCE_HOST.fullmatch(hostname) is None
    ):
        return None
    parts = parsed.path.split("/")
    if (
        len(parts) < 2
        or parts[0]
        or any(part in {"", ".", ".."} for part in parts[1:])
        or any(_SOURCE_PATH_PART.fullmatch(part) is None for part in parts[1:])
    ):
        return None
    if not parts[-1].endswith(".git"):
        if not add_git_suffix:
            return None
        parts[-1] += ".git"
    if parts[-1] == ".git":
        return None
    return urlunsplit(("https", hostname.lower(), "/".join(parts), "", ""))


def _git_environment() -> dict[str, str]:
    """Build an environment that cannot redirect Git outside owned scratch."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GCM_INTERACTIVE": "never",
            "GIT_ASKPASS": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    return environment


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _run_git(
    arguments: list[str],
    *,
    cwd: Path,
    timeout: int = 15,
    max_stdout_bytes: int = _MAX_GIT_TEXT_BYTES,
) -> bytes:
    """Run Git with bounded output and no inherited Git control variables."""

    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [
                "git",
                "-c",
                "credential.helper=",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.https.allow=always",
                *arguments,
            ],
            cwd=cwd,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert process.stdout is not None
        deadline = time.monotonic() + timeout
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _GitFailure
                if not selector.select(remaining):
                    raise _GitFailure
                chunk = os.read(
                    process.stdout.fileno(),
                    min(64 * 1024, max_stdout_bytes + 1 - len(output)),
                )
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > max_stdout_bytes:
                    raise _GitFailure
        remaining = max(0.1, deadline - time.monotonic())
        if process.wait(timeout=remaining) != 0:
            raise _GitFailure
        return bytes(output)
    except _GitFailure:
        if process is not None:
            _stop_process(process)
        raise
    except (OSError, subprocess.SubprocessError) as error:
        if process is not None:
            _stop_process(process)
        raise _GitFailure from error
    finally:
        if process is not None and process.stdout is not None:
            process.stdout.close()


def _git_text(arguments: list[str], *, cwd: Path) -> str:
    try:
        value = _run_git(arguments, cwd=cwd).decode("utf-8", errors="strict").strip()
    except (UnicodeDecodeError, _GitFailure) as error:
        raise _GitFailure from error
    if not value or "\n" in value or "\r" in value:
        raise _GitFailure
    return value


def _directory_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if (
        no_follow is None
        or directory is None
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
        or os.listdir not in os.supports_fd
    ):
        raise ProvenanceError("This platform cannot inspect Autoform provenance safely.")
    return os.O_RDONLY | no_follow | directory | getattr(os, "O_CLOEXEC", 0)


def _stat_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_root(root: Path) -> tuple[Path, int]:
    selected = Path(os.path.abspath(root.expanduser()))
    try:
        before = selected.lstat()
        if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise ProvenanceError("The Autoform plugin root is invalid.")
        descriptor = os.open(selected, _directory_flags())
        opened = os.fstat(descriptor)
        if _stat_signature(opened) != _stat_signature(before):
            os.close(descriptor)
            raise ProvenanceError("The Autoform plugin root changed during inspection.")
    except ProvenanceError:
        raise
    except OSError as error:
        raise ProvenanceError("The Autoform plugin root is unavailable.") from error
    return selected, descriptor


def _require_root_identity(root: Path, descriptor: int) -> None:
    try:
        path_status = root.lstat()
        opened = os.fstat(descriptor)
    except OSError as error:
        raise ProvenanceError("The Autoform plugin root changed during inspection.") from error
    if _stat_signature(path_status) != _stat_signature(opened):
        raise ProvenanceError("The Autoform plugin root changed during inspection.")


def _checkout_candidate(root: Path, root_descriptor: int) -> _Candidate | None:
    try:
        marker = os.stat(".git", dir_fd=root_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ProvenanceError("The Autoform checkout metadata is invalid.") from error
    if not (stat.S_ISDIR(marker.st_mode) or stat.S_ISREG(marker.st_mode)):
        raise ProvenanceError("The Autoform checkout metadata is invalid.")
    try:
        top = Path(_git_text(["rev-parse", "--show-toplevel"], cwd=root))
        top_status = top.stat()
        if (top_status.st_dev, top_status.st_ino) != (
            os.fstat(root_descriptor).st_dev,
            os.fstat(root_descriptor).st_ino,
        ):
            raise ProvenanceError("The Autoform checkout is not rooted at the plugin root.")
        raw_source = _git_text(["remote", "get-url", "origin"], cwd=root)
        revision = _git_text(["rev-parse", "--verify", "HEAD^{commit}"], cwd=root).lower()
    except _GitFailure as error:
        raise ProvenanceError("The Autoform checkout metadata is invalid.") from error
    except OSError as error:
        raise ProvenanceError("The Autoform checkout metadata is invalid.") from error
    source = normalize_git_source(raw_source, allow_github_scp=True, add_git_suffix=True)
    if source is None or _FULL_SHA.fullmatch(revision) is None:
        raise ProvenanceError("The Autoform checkout metadata is invalid.")
    return _Candidate(source=source, revision=revision)


def _read_bounded_regular(
    parent_descriptor: int,
    name: str,
    *,
    limit: int,
    message: str,
) -> tuple[bytes, os.stat_result] | None:
    try:
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ProvenanceError(message) from error
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ProvenanceError(message)
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        try:
            opened = os.fstat(descriptor)
            if _stat_signature(opened) != _stat_signature(before):
                raise ProvenanceError(message)
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            after = os.fstat(descriptor)
            final = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            if (
                len(content) > limit
                or len(content) != opened.st_size
                or _stat_signature(after) != _stat_signature(opened)
                or _stat_signature(final) != _stat_signature(opened)
            ):
                raise ProvenanceError(message)
            return content, opened
        finally:
            os.close(descriptor)
    except ProvenanceError:
        raise
    except OSError as error:
        raise ProvenanceError(message) from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidJson
        result[key] = value
    return result


def _read_install_record(root_descriptor: int) -> _Candidate | None:
    read = _read_bounded_regular(
        root_descriptor,
        INSTALL_RECORD,
        limit=MAX_INSTALL_RECORD_BYTES,
        message="The Autoform installer record is invalid.",
    )
    if read is None:
        return None
    encoded, _ = read
    try:
        payload = json.loads(
            encoded.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, _InvalidJson) as error:
        raise ProvenanceError("The Autoform installer record is invalid.") from error
    if not isinstance(payload, dict):
        raise ProvenanceError("The Autoform installer record is invalid.")
    source_type = payload.get("source_type")
    raw_source = payload.get("source")
    raw_revision = payload.get("revision")
    ref_name = payload.get("ref_name")
    sparse_paths = payload.get("sparse_paths")
    if (
        type(source_type) is not str
        or source_type != "git"
        or type(raw_source) is not str
        or type(raw_revision) is not str
        or type(ref_name) is not str
        or type(sparse_paths) is not list
        or any(type(path) is not str for path in sparse_paths)
    ):
        raise ProvenanceError("The Autoform installer record is invalid.")
    source = normalize_git_source(raw_source, allow_github_scp=True, add_git_suffix=True)
    revision = raw_revision.lower()
    if source is None or _FULL_SHA.fullmatch(revision) is None:
        raise ProvenanceError("The Autoform installer record is invalid.")
    normalized_ref = ref_name.lower()
    if _FULL_SHA.fullmatch(normalized_ref) is not None and normalized_ref != revision:
        raise ProvenanceError("The Autoform installer record conflicts with its revision.")
    return _Candidate(source=source, revision=revision)


def _valid_relative_path(encoded: bytes) -> str:
    relative = os.fsdecode(encoded)
    path = PurePosixPath(relative)
    if (
        not relative
        or relative.startswith("/")
        or "\\" in relative
        or path.is_absolute()
        or len(path.parts) > _MAX_PATH_DEPTH
        or path.as_posix() != relative
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise _GitFailure
    return relative


def _read_git_blob(repository: Path, entry: _TreeObject) -> bytes:
    try:
        size = int(_git_text(["cat-file", "-s", entry.object_id], cwd=repository))
    except (ValueError, _GitFailure) as error:
        raise _GitFailure from error
    if size < 0 or size > _MAX_SHIPPED_FILE_BYTES:
        raise _GitFailure
    content = _run_git(
        ["cat-file", "blob", entry.object_id],
        cwd=repository,
        max_stdout_bytes=size,
    )
    if len(content) != size:
        raise _GitFailure
    return content


def _package_roots(pyproject: bytes) -> tuple[str, ...]:
    try:
        project = tomllib.loads(pyproject.decode("utf-8", errors="strict"))
        name = project["project"]["name"]
        entry_point = project["project"]["scripts"]["autoform"]
        packages = project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    except (KeyError, TypeError, UnicodeDecodeError, ValueError) as error:
        raise _GitFailure from error
    if name != "autoform" or entry_point != "autoform_cli.__main__:main":
        raise _GitFailure
    if type(packages) is not list or any(type(path) is not str for path in packages):
        raise _GitFailure
    roots: list[str] = []
    for raw in packages:
        path = PurePosixPath(raw)
        if (
            not raw
            or raw.startswith("/")
            or "\\" in raw
            or len(path.parts) != 1
            or path.as_posix() != raw
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise _GitFailure
        roots.append(raw)
    if "autoform_cli" not in roots or len(set(roots)) != len(roots):
        raise _GitFailure
    return tuple(sorted(roots))


def _under_root(relative: str, root: str) -> bool:
    return relative == root or relative.startswith(f"{root}/")


def _fetch_source_layout(source: str, revision: str, scratch: Path) -> _SourceLayout:
    repository = scratch / "repository.git"
    _run_git(["init", "--bare", "--template=", str(repository)], cwd=scratch)
    _run_git(
        ["fetch", "--no-tags", "--no-recurse-submodules", "--depth=1", source, revision],
        cwd=repository,
        timeout=60,
        max_stdout_bytes=1024 * 1024,
    )
    resolved = _git_text(["rev-parse", "--verify", "FETCH_HEAD^{commit}"], cwd=repository).lower()
    if resolved != revision:
        raise _GitFailure
    listing = _run_git(
        ["ls-tree", "-rz", "--full-tree", resolved],
        cwd=repository,
        max_stdout_bytes=_MAX_GIT_LIST_BYTES,
    )
    objects: dict[str, _TreeObject] = {}
    for raw_entry in listing.split(b"\0"):
        if not raw_entry:
            continue
        if len(objects) >= _MAX_MANIFEST_ENTRIES:
            raise _GitFailure
        try:
            raw_header, raw_path = raw_entry.split(b"\t", 1)
            raw_mode, raw_kind, raw_object = raw_header.split(b" ", 2)
            mode = int(raw_mode, 8)
            kind = raw_kind.decode("ascii")
            object_id = raw_object.decode("ascii")
        except (UnicodeDecodeError, ValueError) as error:
            raise _GitFailure from error
        relative = _valid_relative_path(raw_path)
        if relative in objects:
            raise _GitFailure
        objects[relative] = _TreeObject(mode=mode, kind=kind, object_id=object_id)

    pyproject_object = objects.get("pyproject.toml")
    if pyproject_object is None or pyproject_object.kind != "blob" or pyproject_object.mode != 0o100644:
        raise _GitFailure
    pyproject = _read_git_blob(repository, pyproject_object)
    package_roots = _package_roots(pyproject)
    optional_roots = {
        root
        for root in _OPTIONAL_SHIPPED_ROOTS
        if any(_under_root(path, root) for path in objects)
    }
    roots = tuple(sorted(set((*_SHIPPED_ROOTS, *optional_roots, *package_roots))))
    for root in roots:
        if not any(_under_root(path, root) for path in objects):
            raise _GitFailure
    if not _SHIPPED_FILES.issubset(objects):
        raise _GitFailure

    manifest: dict[str, _ManifestEntry] = {}
    total = 0
    for relative, tree_object in sorted(objects.items()):
        in_boundary = relative in _SHIPPED_FILES or any(
            _under_root(relative, root) for root in roots
        )
        if not in_boundary or PurePosixPath(relative).name == ".DS_Store":
            continue
        path = PurePosixPath(relative)
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            raise _GitFailure
        if tree_object.kind != "blob" or tree_object.mode not in {0o100644, 0o100755}:
            raise _GitFailure
        content = pyproject if relative == "pyproject.toml" else _read_git_blob(repository, tree_object)
        total += len(content)
        if total > _MAX_SHIPPED_TOTAL_BYTES:
            raise _GitFailure
        manifest[relative] = _ManifestEntry(mode=tree_object.mode, content=content)
    return _SourceLayout(
        files=manifest,
        all_files=frozenset(objects),
        roots=roots,
        package_roots=package_roots,
    )


def _safe_names(directory_descriptor: int, counter: list[int]) -> list[str]:
    try:
        names = os.listdir(directory_descriptor)
    except OSError as error:
        raise ProvenanceError("The installed Autoform files could not be inspected safely.") from error
    counter[0] += len(names)
    if counter[0] > _MAX_MANIFEST_ENTRIES:
        raise ProvenanceError("The installed Autoform tree is too large to verify safely.")
    if any(
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        for name in names
    ):
        raise ProvenanceError("The installed Autoform tree contains an invalid path.")
    return sorted(names)


def _open_child_directory(parent_descriptor: int, name: str, before: os.stat_result) -> int:
    try:
        child = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
        opened = os.fstat(child)
    except OSError as error:
        raise ProvenanceError("The installed Autoform files could not be inspected safely.") from error
    if _stat_signature(opened) != _stat_signature(before):
        os.close(child)
        raise ProvenanceError("The installed Autoform tree changed during inspection.")
    return child


def _require_child_identity(parent_descriptor: int, name: str, descriptor: int) -> None:
    try:
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        opened = os.fstat(descriptor)
    except OSError as error:
        raise ProvenanceError("The installed Autoform tree changed during inspection.") from error
    if _stat_signature(current) != _stat_signature(opened):
        raise ProvenanceError("The installed Autoform tree changed during inspection.")


def _read_actual_file(
    parent_descriptor: int,
    name: str,
    budget: list[int],
) -> _ActualEntry:
    read = _read_bounded_regular(
        parent_descriptor,
        name,
        limit=_MAX_SHIPPED_FILE_BYTES,
        message="The installed Autoform files do not match the recorded commit.",
    )
    if read is None:
        raise ProvenanceError("The installed Autoform files do not match the recorded commit.")
    content, metadata = read
    budget[0] += len(content)
    if budget[0] > _MAX_SHIPPED_TOTAL_BYTES:
        raise ProvenanceError("The installed Autoform tree is too large to verify safely.")
    mode = 0o100755 if metadata.st_mode & 0o111 else 0o100644
    return _ActualEntry(
        mode=mode,
        content=content,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
    )


def _scan_pycache(
    parent_descriptor: int,
    name: str,
    before: os.stat_result,
    *,
    source_parent: str,
    counter: list[int],
    budget: list[int],
    bytecode: list[_CachedBytecode],
) -> None:
    descriptor = _open_child_directory(parent_descriptor, name, before)
    try:
        for child_name in _safe_names(descriptor, counter):
            try:
                metadata = os.stat(child_name, dir_fd=descriptor, follow_symlinks=False)
            except OSError as error:
                raise ProvenanceError("The installed bytecode cache is invalid.") from error
            if not stat.S_ISREG(metadata.st_mode) or not child_name.endswith(".pyc"):
                raise ProvenanceError("The installed bytecode cache is invalid.")
            entry = _read_actual_file(descriptor, child_name, budget)
            if entry.mode != 0o100644:
                raise ProvenanceError("The installed bytecode cache is invalid.")
            bytecode.append(_CachedBytecode(source_parent, child_name, entry.content))
        _require_child_identity(parent_descriptor, name, descriptor)
    finally:
        os.close(descriptor)


def _scan_boundary_directory(
    descriptor: int,
    prefix: str,
    *,
    files: dict[str, _ActualEntry],
    directories: set[str],
    bytecode: list[_CachedBytecode],
    counter: list[int],
    budget: list[int],
    depth: int,
) -> None:
    if depth > _MAX_PATH_DEPTH:
        raise ProvenanceError("The installed Autoform tree is too deep to verify safely.")
    for name in _safe_names(descriptor, counter):
        relative = f"{prefix}/{name}" if prefix else name
        try:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as error:
            raise ProvenanceError("The installed Autoform files could not be inspected safely.") from error
        if name == ".DS_Store" and stat.S_ISREG(metadata.st_mode):
            continue
        if stat.S_ISDIR(metadata.st_mode):
            if name == "__pycache__":
                _scan_pycache(
                    descriptor,
                    name,
                    metadata,
                    source_parent=prefix,
                    counter=counter,
                    budget=budget,
                    bytecode=bytecode,
                )
                continue
            directories.add(relative)
            child = _open_child_directory(descriptor, name, metadata)
            try:
                _scan_boundary_directory(
                    child,
                    relative,
                    files=files,
                    directories=directories,
                    bytecode=bytecode,
                    counter=counter,
                    budget=budget,
                    depth=depth + 1,
                )
                _require_child_identity(descriptor, name, child)
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ProvenanceError("The installed Autoform tree contains a link or special file.")
        files[relative] = _read_actual_file(descriptor, name, budget)


def _open_boundary_root(
    root_descriptor: int,
    relative: str,
    directories: set[str],
) -> tuple[int, list[tuple[int, str, int]]]:
    descriptor = os.dup(root_descriptor)
    opened: list[tuple[int, str, int]] = []
    prefix: list[str] = []
    try:
        for name in PurePosixPath(relative).parts:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ProvenanceError("The installed Autoform files do not match the recorded commit.")
            child = _open_child_directory(descriptor, name, metadata)
            opened.append((descriptor, name, child))
            prefix.append(name)
            directories.add("/".join(prefix))
            descriptor = child
        return descriptor, opened
    except (OSError, ProvenanceError):
        for parent, _, child in reversed(opened):
            os.close(child)
            os.close(parent)
        if not opened:
            os.close(descriptor)
        raise ProvenanceError("The installed Autoform files do not match the recorded commit.") from None


def _close_boundary_root(opened: list[tuple[int, str, int]]) -> None:
    for parent, name, child in reversed(opened):
        try:
            _require_child_identity(parent, name, child)
        finally:
            os.close(child)
            os.close(parent)


def _expected_directories(files: dict[str, _ManifestEntry]) -> set[str]:
    directories: set[str] = set()
    for relative in files:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _validate_current_bytecode(
    root: Path,
    cached: _CachedBytecode,
    source_relative: str,
    source: _ActualEntry,
    expected_source: bytes,
    optimization: int,
) -> None:
    content = cached.content
    if len(content) < 16 or content[:4] != importlib.util.MAGIC_NUMBER:
        raise ProvenanceError("The installed bytecode cache does not match its source.")
    flags = int.from_bytes(content[4:8], "little")
    if flags == 0:
        timestamp = int.from_bytes(content[8:12], "little")
        source_size = int.from_bytes(content[12:16], "little")
        if timestamp != (int(source.mtime_ns // 1_000_000_000) & 0xFFFFFFFF):
            raise ProvenanceError("The installed bytecode cache does not match its source.")
        if source_size != (source.size & 0xFFFFFFFF):
            raise ProvenanceError("The installed bytecode cache does not match its source.")
    elif flags == 3:
        if content[8:16] != importlib.util.source_hash(expected_source):
            raise ProvenanceError("The installed bytecode cache does not match its source.")
    else:
        # Unchecked-hash bytecode can supersede the verified source by design.
        raise ProvenanceError("The installed bytecode cache does not match its source.")
    source_path = root.joinpath(*PurePosixPath(source_relative).parts)
    try:
        with tempfile.TemporaryDirectory(prefix="autoform-bytecode-") as directory:
            temporary_source = Path(directory, "source.py")
            temporary_cache = Path(directory, "source.pyc")
            temporary_source.write_bytes(expected_source)
            py_compile.compile(
                os.fspath(temporary_source),
                cfile=os.fspath(temporary_cache),
                dfile=os.fspath(source_path),
                doraise=True,
                optimize=optimization,
                invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
            )
            expected_payload = temporary_cache.read_bytes()[16:]
    except (MemoryError, OSError, OverflowError, py_compile.PyCompileError) as error:
        raise ProvenanceError("The installed bytecode cache could not be verified.") from error
    if content[16:] != expected_payload:
        raise ProvenanceError("The installed bytecode cache does not match its source.")


def _validate_bytecode(
    root: Path,
    bytecode: list[_CachedBytecode],
    actual: dict[str, _ActualEntry],
    expected: dict[str, _ManifestEntry],
) -> None:
    current_tag = sys.implementation.cache_tag
    if not current_tag:
        raise ProvenanceError("The installed bytecode cache cannot be verified.")
    for cached in bytecode:
        match = _BYTECODE_NAME.fullmatch(cached.name)
        if match is None:
            raise ProvenanceError("The installed bytecode cache is invalid.")
        source_relative = PurePosixPath(cached.parent, f"{match.group('stem')}.py").as_posix()
        expected_entry = expected.get(source_relative)
        actual_entry = actual.get(source_relative)
        if expected_entry is None or actual_entry is None:
            raise ProvenanceError("The installed bytecode cache has no verified source.")
        if match.group("tag") != current_tag:
            continue
        raw_optimization = match.group("optimization")
        if raw_optimization is None:
            optimization = 0
        elif raw_optimization in {"1", "2"}:
            optimization = int(raw_optimization)
        else:
            raise ProvenanceError("The installed bytecode cache is invalid.")
        _validate_current_bytecode(
            root,
            cached,
            source_relative,
            actual_entry,
            expected_entry.content,
            optimization,
        )


def _is_derived_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    return (
        path.parts[0] in _DERIVED_ROOTS
        or any(part in _DERIVED_DIRECTORY_NAMES for part in path.parts)
        or path.name in _DERIVED_FILE_NAMES
        or relative == INSTALL_RECORD
    )


def _looks_importable(relative: str) -> bool:
    name = PurePosixPath(relative).name
    return any(name.endswith(suffix) for suffix in _IMPORTABLE_SUFFIXES)


def _scan_for_extra_importable(
    descriptor: int,
    prefix: str,
    *,
    layout: _SourceLayout,
    counter: list[int],
    depth: int,
) -> None:
    if depth > _MAX_PATH_DEPTH:
        raise ProvenanceError("The installed Autoform tree is too deep to verify safely.")
    for name in _safe_names(descriptor, counter):
        relative = f"{prefix}/{name}" if prefix else name
        if any(_under_root(relative, root) for root in layout.roots):
            continue
        if _is_derived_path(relative):
            continue
        try:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as error:
            raise ProvenanceError("The installed Autoform files could not be inspected safely.") from error
        if stat.S_ISDIR(metadata.st_mode):
            child = _open_child_directory(descriptor, name, metadata)
            try:
                _scan_for_extra_importable(
                    child,
                    relative,
                    layout=layout,
                    counter=counter,
                    depth=depth + 1,
                )
                _require_child_identity(descriptor, name, child)
            finally:
                os.close(child)
        elif relative not in layout.all_files and (
            not stat.S_ISREG(metadata.st_mode) or _looks_importable(relative)
        ):
            raise ProvenanceError("The installed Autoform tree contains extra importable code.")


def _compare_installed_tree(
    root: Path,
    root_descriptor: int,
    layout: _SourceLayout,
) -> None:
    actual_files: dict[str, _ActualEntry] = {}
    actual_directories: set[str] = set()
    bytecode: list[_CachedBytecode] = []
    counter = [0]
    budget = [0]

    roots: list[str] = []
    for candidate in sorted(layout.roots, key=lambda value: (len(PurePosixPath(value).parts), value)):
        if not any(_under_root(candidate, selected) for selected in roots):
            roots.append(candidate)
    for relative in roots:
        descriptor, opened = _open_boundary_root(root_descriptor, relative, actual_directories)
        try:
            _scan_boundary_directory(
                descriptor,
                relative,
                files=actual_files,
                directories=actual_directories,
                bytecode=bytecode,
                counter=counter,
                budget=budget,
                depth=len(PurePosixPath(relative).parts),
            )
        finally:
            _close_boundary_root(opened)

    for relative in _SHIPPED_FILES:
        if len(PurePosixPath(relative).parts) != 1:
            raise ProvenanceError("The installed Autoform boundary is invalid.")
        actual_files[relative] = _read_actual_file(root_descriptor, relative, budget)

    expected_directories = _expected_directories(layout.files)
    if set(actual_files) != set(layout.files) or actual_directories != expected_directories:
        raise ProvenanceError("The installed Autoform tree does not match the recorded commit.")
    for relative, expected in layout.files.items():
        found = actual_files[relative]
        if found.mode != expected.mode or found.content != expected.content:
            raise ProvenanceError("The installed Autoform files do not match the recorded commit.")
    _validate_bytecode(root, bytecode, actual_files, layout.files)
    _scan_for_extra_importable(
        root_descriptor,
        "",
        layout=layout,
        counter=[0],
        depth=0,
    )


def verify_plugin_provenance(root: Path | None = None) -> PluginProvenance:
    """Verify the source, remote commit, and installed plugin before returning."""

    selected_root, root_descriptor = _open_root(root or plugin_root())
    try:
        checkout = _checkout_candidate(selected_root, root_descriptor)
        record = _read_install_record(root_descriptor)
        if checkout is not None and record is not None and checkout != record:
            raise ProvenanceError("The Autoform checkout and installer record conflict.")
        candidate = checkout or record
        if candidate is None:
            raise ProvenanceError("No trustworthy Autoform source and commit are available.")
        _require_root_identity(selected_root, root_descriptor)
        try:
            with tempfile.TemporaryDirectory(prefix="autoform-provenance-") as temporary:
                layout = _fetch_source_layout(
                    candidate.source,
                    candidate.revision,
                    Path(temporary),
                )
                _compare_installed_tree(selected_root, root_descriptor, layout)
                # Re-read the complete boundary before committing the result.
                # A mutation after an earlier root was scanned must not be
                # hidden by that root's unchanged parent-directory identity.
                _compare_installed_tree(selected_root, root_descriptor, layout)
        except ProvenanceError:
            raise
        except (_GitFailure, OSError) as error:
            raise ProvenanceError("The recorded Autoform commit could not be verified.") from error
        _require_root_identity(selected_root, root_descriptor)
        return PluginProvenance(source=candidate.source, revision=candidate.revision)
    finally:
        os.close(root_descriptor)


def plugin_pin() -> tuple[str, str]:
    """Compatibility tuple for callers that need all-or-nothing provenance."""

    try:
        provenance = verify_plugin_provenance()
    except ProvenanceError:
        return "", ""
    return provenance.source, provenance.revision


__all__ = [
    "INSTALL_RECORD",
    "MAX_INSTALL_RECORD_BYTES",
    "PluginProvenance",
    "ProvenanceError",
    "normalize_git_source",
    "plugin_pin",
    "plugin_root",
    "verify_plugin_provenance",
]
