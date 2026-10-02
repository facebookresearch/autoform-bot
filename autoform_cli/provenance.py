"""Resolve and verify immutable provenance for the running Autoform plugin.

The source and revision emitted here are persisted in generated workflows. A
candidate is therefore returned only when its remote commit is obtainable and
the complete installed tracked tree matches that commit, apart from narrowly
validated host metadata.
"""

from __future__ import annotations

import importlib.util
import json
import os
import py_compile
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psutil
from packaging.requirements import InvalidRequirement, Requirement

from .bounded_toml import BoundedTomlError, loads_bounded_toml


INSTALL_RECORD = ".codex-marketplace-install.json"
MAX_INSTALL_RECORD_BYTES = 64 * 1024
MAX_CLAUDE_REGISTRY_BYTES = 1024 * 1024

_MAX_GIT_TEXT_BYTES = 16 * 1024
_MAX_GIT_LIST_BYTES = 8 * 1024 * 1024
_MAX_MANIFEST_ENTRIES = 20_000
_MAX_SHIPPED_FILE_BYTES = 16 * 1024 * 1024
_MAX_SHIPPED_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_PATH_DEPTH = 64
_MAX_JSON_DEPTH = 128
_MAX_TOML_DEPTH = 128

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
_LOCK_HASH = re.compile(r"sha256:[0-9a-f]{64}")

# These roots and files are required in every supported Autoform source tree.
# Provenance comparison covers every tracked blob; these names additionally
# define the minimum plugin contract and package metadata used to validate it.
_SHIPPED_ROOTS = frozenset(
    {
        ".claude-plugin",
        ".codex-plugin",
        ".muse-plugin",
        "assets",
        "skills",
    }
)
_OPTIONAL_SHIPPED_ROOTS = frozenset(
    {
        "agents",
        "bin",
        "commands",
        "hooks",
        "monitors",
        "output-styles",
        "scripts",
        "themes",
        "workflows",
    }
)
_SHIPPED_FILES = frozenset({".mcp.json", "pyproject.toml", "uv.lock"})
_OPTIONAL_SHIPPED_FILES = frozenset(
    {
        ".app.json",
        ".lsp.json",
        ".npmrc",
        "bun.lock",
        "bun.lockb",
        "bunfig.toml",
        "mcp.json",
        "npm-shrinkwrap.json",
        "package-lock.json",
        "package.json",
        "plugin.json",
        "settings.json",
    }
)
_FORBIDDEN_RUNTIME_FILES = frozenset({".python-version", "uv.toml"})
_FORBIDDEN_RUNTIME_ROOTS = frozenset({".venv"})
_CLAUDE_REWRITTEN_MANIFESTS = frozenset(
    {".claude-plugin/plugin.json", ".muse-plugin/plugin.json"}
)
_CLAUDE_BUILD_COMMIT = "BUILD_COMMIT"
_CLAUDE_BASE_VERSION = re.compile(
    r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)

# These are host- or tool-owned state rather than source.  The exact list is
# deliberately local; arbitrary gitignored paths are not automatically trusted.
_DERIVED_ROOTS = frozenset(
    {
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
_CLAUDE_PLUGIN_REGISTRY = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
_CLAUDE_MARKETPLACE_REGISTRY = Path.home() / ".claude" / "plugins" / "known_marketplaces.json"


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
    installed_version: str | None = field(default=None, compare=False)
    cache_version: str | None = field(default=None, compare=False)
    registry_revision: str | None = field(default=None, compare=False)


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


def _git_environment(home: Path) -> dict[str, str]:
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
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": os.fspath(home),
            "LC_ALL": "C",
            "NETRC": os.devnull,
            "USERPROFILE": os.fspath(home),
            "XDG_CONFIG_DIRS": os.fspath(home),
            "XDG_CONFIG_HOME": os.fspath(home),
        }
    )
    return environment


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            if process.poll() is None:
                process.kill()
    elif process.poll() is None:
        try:
            descendants = psutil.Process(process.pid).children(recursive=True)
        except (psutil.Error, OSError):
            descendants = []
        for descendant in reversed(descendants):
            try:
                descendant.kill()
            except psutil.Error:
                pass
        process.kill()
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _start_git(
    arguments: list[str], *, cwd: Path, stdin: Any
) -> tuple[subprocess.Popen[bytes], tempfile.TemporaryDirectory[str]]:
    """Start one isolated Git process in its own killable process group."""

    git_home = tempfile.TemporaryDirectory(prefix="autoform-git-home-")
    popen_options: dict[str, object] = {}
    if os.name == "posix":
        popen_options["start_new_session"] = True
    elif os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
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
            env=_git_environment(Path(git_home.name)),
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **popen_options,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        git_home.cleanup()
        raise _GitFailure from error
    return process, git_home


def _run_git(
    arguments: list[str],
    *,
    cwd: Path,
    timeout: float = 15,
    deadline: float | None = None,
    max_stdout_bytes: int = _MAX_GIT_TEXT_BYTES,
    stdin_bytes: bytes | None = None,
) -> bytes:
    """Run Git with bounded output and no inherited Git control variables."""

    process: subprocess.Popen[bytes] | None = None
    request_file: Any = None
    git_home: tempfile.TemporaryDirectory[str] | None = None
    try:
        operation_deadline = time.monotonic() + timeout if deadline is None else deadline
        if operation_deadline <= time.monotonic() or max_stdout_bytes < 0:
            raise _GitFailure
        if stdin_bytes is not None:
            if len(stdin_bytes) > _MAX_GIT_LIST_BYTES:
                raise _GitFailure
            request_file = tempfile.TemporaryFile()
            request_file.write(stdin_bytes)
            request_file.seek(0)
        process, git_home = _start_git(
            arguments,
            cwd=cwd,
            stdin=request_file if request_file is not None else subprocess.DEVNULL,
        )
        assert process.stdout is not None
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = operation_deadline - time.monotonic()
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
        remaining = operation_deadline - time.monotonic()
        if remaining <= 0:
            raise _GitFailure
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
    except BaseException:
        if process is not None:
            _stop_process(process)
        raise
    finally:
        if process is not None and process.stdout is not None:
            process.stdout.close()
        if request_file is not None:
            request_file.close()
        if git_home is not None:
            git_home.cleanup()


def _git_text(arguments: list[str], *, cwd: Path, deadline: float | None = None) -> str:
    try:
        value = _run_git(arguments, cwd=cwd, deadline=deadline).decode("utf-8", errors="strict").strip()
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


def _decode_json_object(encoded: bytes, message: str) -> dict[str, Any]:
    def reject_constant(_value: str) -> None:
        raise _InvalidJson

    try:
        payload = json.loads(
            encoded.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_float=Decimal,
            parse_constant=reject_constant,
        )
    except (
        UnicodeDecodeError,
        ValueError,
        RecursionError,
        MemoryError,
        DecimalException,
        _InvalidJson,
    ) as error:
        raise ProvenanceError(message) from error
    if not isinstance(payload, dict):
        raise ProvenanceError(message)
    return payload


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
    payload = _decode_json_object(encoded, "The Autoform installer record is invalid.")
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
        or (ref_name is not None and type(ref_name) is not str)
        or type(sparse_paths) is not list
        or any(type(path) is not str for path in sparse_paths)
    ):
        raise ProvenanceError("The Autoform installer record is invalid.")
    source = normalize_git_source(raw_source, allow_github_scp=True, add_git_suffix=True)
    revision = raw_revision.lower()
    if source is None or _FULL_SHA.fullmatch(revision) is None:
        raise ProvenanceError("The Autoform installer record is invalid.")
    if ref_name is not None:
        normalized_ref = ref_name.lower()
        if _FULL_SHA.fullmatch(normalized_ref) is not None and normalized_ref != revision:
            raise ProvenanceError("The Autoform installer record conflicts with its revision.")
    return _Candidate(source=source, revision=revision)


def _read_bounded_path(path: Path, *, message: str) -> bytes | None:
    """Read one external registry without following its directory or file link."""

    selected = Path(os.path.abspath(path.expanduser()))
    try:
        parent_descriptor = os.open(selected.parent, _directory_flags())
    except OSError as error:
        raise ProvenanceError(message) from error
    try:
        read = _read_bounded_regular(
            parent_descriptor,
            selected.name,
            limit=MAX_CLAUDE_REGISTRY_BYTES,
            message=message,
        )
    finally:
        os.close(parent_descriptor)
    return None if read is None else read[0]


def _claude_registry_paths() -> tuple[Path, Path]:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if not configured:
        return _CLAUDE_PLUGIN_REGISTRY, _CLAUDE_MARKETPLACE_REGISTRY
    message = "The Claude configuration directory is invalid."
    if (
        len(configured) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in configured)
    ):
        raise ProvenanceError(message)
    try:
        root = Path(os.path.abspath(Path(configured).expanduser()))
        os.fsencode(root)
    except (OSError, RuntimeError, UnicodeError, ValueError) as error:
        raise ProvenanceError(message) from error
    plugins = root / "plugins"
    return plugins / "installed_plugins.json", plugins / "known_marketplaces.json"


def _claude_cache_coordinates(
    root: Path, plugin_registry: Path | None = None
) -> tuple[str, str, str] | None:
    if plugin_registry is None:
        plugin_registry, _ = _claude_registry_paths()
    cache_root = Path(os.path.abspath(plugin_registry.parent / "cache"))
    try:
        relative = root.relative_to(cache_root)
    except ValueError:
        return None
    if len(relative.parts) != 3 or relative.parts[1] != "autoform":
        return None
    marketplace, plugin, version = relative.parts
    if any(
        not component
        or component in {".", ".."}
        or len(component) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in component)
        for component in (marketplace, plugin, version)
    ):
        return None
    return marketplace, plugin, version


def _absolute_registry_path(value: object, message: str) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ProvenanceError(message)
    try:
        path = Path(value).expanduser()
        os.fsencode(path)
    except (OSError, RuntimeError, UnicodeError, ValueError) as error:
        raise ProvenanceError(message) from error
    if not path.is_absolute():
        raise ProvenanceError(message)
    return Path(os.path.abspath(path))


def _claude_install_metadata(
    root: Path,
    marketplace: str,
    plugin: str,
    plugin_registry: Path | None = None,
) -> tuple[str | None, str | None]:
    message = "The Claude plugin installation registry is invalid."
    if plugin_registry is None:
        plugin_registry, _ = _claude_registry_paths()
    encoded = _read_bounded_path(plugin_registry, message=message)
    if encoded is None:
        return None, None
    payload = _decode_json_object(encoded, message)
    plugins = payload.get("plugins")
    if not isinstance(plugins, dict):
        raise ProvenanceError(message)
    entries = plugins.get(f"{plugin}@{marketplace}")
    if entries is None:
        return None, None
    if not isinstance(entries, list):
        raise ProvenanceError(message)
    metadata: list[tuple[str | None, str | None]] = []
    matching_entries = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ProvenanceError(message)
        install_path = entry.get("installPath")
        installed = _absolute_registry_path(install_path, message)
        if installed != root:
            continue
        matching_entries += 1
        revision = entry.get("gitCommitSha")
        version = entry.get("version")
        if revision is None and version is None:
            continue
        if revision is not None and not isinstance(revision, str):
            raise ProvenanceError(message)
        normalized_revision = revision.lower() if revision is not None else None
        if normalized_revision is not None and _FULL_SHA.fullmatch(normalized_revision) is None:
            raise ProvenanceError(message)
        if version is not None and (
            type(version) is not str
            or not version
            or len(version) > 255
            or version != version.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in version)
        ):
            raise ProvenanceError(message)
        metadata.append((normalized_revision, version))
    if matching_entries > 1 or len(metadata) > 1:
        raise ProvenanceError(message)
    return metadata[0] if metadata else (None, None)


def _claude_install_revision(
    root: Path,
    marketplace: str,
    plugin: str,
    plugin_registry: Path | None = None,
) -> str | None:
    revision, _ = _claude_install_metadata(root, marketplace, plugin, plugin_registry)
    return revision


def _claude_marketplace_candidate(root: Path) -> _Candidate | None:
    plugin_registry, marketplace_registry = _claude_registry_paths()
    coordinates = _claude_cache_coordinates(root, plugin_registry)
    if coordinates is None:
        return None
    marketplace, plugin, cache_version = coordinates
    revision, installed_version = _claude_install_metadata(
        root, marketplace, plugin, plugin_registry
    )

    message = "The Claude plugin marketplace registry is invalid."
    encoded = _read_bounded_path(marketplace_registry, message=message)
    if encoded is None:
        return None
    payload = _decode_json_object(encoded, message)
    entry = payload.get(marketplace)
    if not isinstance(entry, dict):
        raise ProvenanceError(message)
    checkout_path = _absolute_registry_path(entry.get("installLocation"), message)
    try:
        selected_checkout, checkout_descriptor = _open_root(checkout_path)
        try:
            checkout = _checkout_candidate(selected_checkout, checkout_descriptor)
        finally:
            os.close(checkout_descriptor)
    except ProvenanceError as error:
        raise ProvenanceError(message) from error
    if checkout is None:
        raise ProvenanceError(message)
    return _Candidate(
        checkout.source,
        revision or checkout.revision,
        installed_version=installed_version,
        cache_version=cache_version,
        registry_revision=revision,
    )


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


def _object_request(object_ids: Iterable[str]) -> bytes:
    unique = sorted(set(object_ids))
    if (
        len(unique) > _MAX_MANIFEST_ENTRIES
        or any(_FULL_SHA.fullmatch(object_id) is None for object_id in unique)
    ):
        raise _GitFailure
    encoded = b"".join(f"{object_id}\n".encode("ascii") for object_id in unique)
    if len(encoded) > _MAX_GIT_LIST_BYTES:
        raise _GitFailure
    return encoded


def _require_blob_presence(
    repository: Path,
    objects: Iterable[_TreeObject],
    expected: Iterable[str],
    *,
    deadline: float,
) -> None:
    """Require exactly ``expected`` blobs to exist without lazy network access."""

    object_ids = sorted({entry.object_id for entry in objects if entry.kind == "blob"})
    expected_ids = set(expected)
    if not expected_ids.issubset(object_ids):
        raise _GitFailure
    output = _run_git(
        [
            "cat-file",
            "--batch-check=%(objectname) %(objecttype) %(objectsize)",
            "--batch-all-objects",
            "--unordered",
        ],
        cwd=repository,
        deadline=deadline,
        max_stdout_bytes=_MAX_GIT_LIST_BYTES,
    )
    present_ids: set[str] = set()
    for line in output.splitlines():
        fields = line.split(b" ")
        if len(fields) != 3 or not fields[2].isdigit():
            raise _GitFailure
        try:
            object_id = fields[0].decode("ascii")
            kind = fields[1].decode("ascii")
        except UnicodeDecodeError as error:
            raise _GitFailure from error
        if _FULL_SHA.fullmatch(object_id) is None:
            raise _GitFailure
        if kind == "blob":
            if object_id in present_ids:
                raise _GitFailure
            present_ids.add(object_id)
    if present_ids != expected_ids:
        raise _GitFailure


def _fetch_git_objects(repository: Path, object_ids: Iterable[str], *, deadline: float) -> None:
    """Fetch only selected promised blobs through Git's partial-clone transport."""

    request = _object_request(object_ids)
    if not request:
        return
    _run_git(
        [
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            "--recurse-submodules=no",
            "--filter=blob:none",
            "--stdin",
            "origin",
        ],
        cwd=repository,
        deadline=deadline,
        max_stdout_bytes=1024 * 1024,
        stdin_bytes=request,
    )


def _read_git_blobs(
    repository: Path,
    objects: Iterable[_TreeObject],
    *,
    deadline: float,
    known: dict[str, bytes] | None = None,
) -> dict[str, bytes]:
    """Read unique local blobs through one bounded ``cat-file --batch`` process."""

    contents = dict(known or {})
    entries = tuple(objects)
    if any(entry.kind != "blob" for entry in entries):
        raise _GitFailure
    requested = sorted({entry.object_id for entry in entries if entry.object_id not in contents})
    request = _object_request(requested)
    total = sum(len(content) for content in contents.values())
    if total > _MAX_SHIPPED_TOTAL_BYTES:
        raise _GitFailure
    if not requested:
        return contents

    process: subprocess.Popen[bytes] | None = None
    request_file: Any = None
    git_home: tempfile.TemporaryDirectory[str] | None = None
    try:
        if deadline <= time.monotonic():
            raise _GitFailure
        request_file = tempfile.TemporaryFile()
        request_file.write(request)
        request_file.seek(0)
        process, git_home = _start_git(
            ["cat-file", "--batch"],
            cwd=repository,
            stdin=request_file,
        )
        assert process.stdout is not None
        buffer = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)

            def read_more() -> bool:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise _GitFailure
                chunk = os.read(process.stdout.fileno(), 64 * 1024)
                if not chunk:
                    return False
                buffer.extend(chunk)
                return True

            def read_line() -> bytes:
                while True:
                    end = buffer.find(b"\n")
                    if end >= 0:
                        if end > 128:
                            raise _GitFailure
                        line = bytes(buffer[:end])
                        del buffer[: end + 1]
                        return line
                    if len(buffer) > 128 or not read_more():
                        raise _GitFailure

            def read_exact(size: int) -> bytes:
                while len(buffer) < size:
                    if not read_more():
                        raise _GitFailure
                value = bytes(buffer[:size])
                del buffer[:size]
                return value

            for object_id in requested:
                fields = read_line().split(b" ")
                if (
                    len(fields) != 3
                    or fields[0] != object_id.encode("ascii")
                    or fields[1] != b"blob"
                    or not fields[2].isdigit()
                ):
                    raise _GitFailure
                size = int(fields[2])
                if size > _MAX_SHIPPED_FILE_BYTES or total + size > _MAX_SHIPPED_TOTAL_BYTES:
                    raise _GitFailure
                content = read_exact(size)
                if read_exact(1) != b"\n":
                    raise _GitFailure
                contents[object_id] = content
                total += size

            if buffer or read_more():
                raise _GitFailure
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise _GitFailure
        return contents
    except _GitFailure:
        if process is not None:
            _stop_process(process)
        raise
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        if process is not None:
            _stop_process(process)
        raise _GitFailure from error
    except BaseException:
        if process is not None:
            _stop_process(process)
        raise
    finally:
        if process is not None and process.stdout is not None:
            process.stdout.close()
        if request_file is not None:
            request_file.close()
        if git_home is not None:
            git_home.cleanup()


def _require_remote_python_requirements(value: object) -> None:
    if type(value) is not list:
        raise _GitFailure
    for specification in value:
        if (
            type(specification) is not str
            or not specification
            or specification != specification.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in specification)
        ):
            raise _GitFailure
        try:
            requirement = Requirement(specification)
        except InvalidRequirement as error:
            raise _GitFailure from error
        if requirement.url is not None:
            raise _GitFailure


def _package_roots(pyproject: bytes) -> tuple[str, ...]:
    try:
        project = loads_bounded_toml(
            pyproject.decode("utf-8", errors="strict"),
            max_depth=_MAX_TOML_DEPTH,
        )
        build_system = project["build-system"]
        metadata = project["project"]
        name = metadata["name"]
        entry_point = metadata["scripts"]["autoform"]
        tool = project["tool"]
        hatch = tool["hatch"]
        build = hatch["build"]
        targets = build["targets"]
        wheel = targets["wheel"]
        packages = wheel["packages"]
    except (BoundedTomlError, KeyError, TypeError, UnicodeDecodeError) as error:
        raise _GitFailure from error
    if (
        type(build_system) is not dict
        or set(build_system) != {"build-backend", "requires"}
        or build_system["build-backend"] != "hatchling.build"
        or type(metadata) is not dict
        or type(tool) is not dict
        or "uv" in tool
        or "dependency-groups" in project
        or type(hatch) is not dict
        or set(hatch) != {"build"}
        or type(build) is not dict
        or set(build) != {"targets"}
        or type(targets) is not dict
        or set(targets) != {"wheel"}
        or type(wheel) is not dict
        or set(wheel) != {"packages"}
        or name != "autoform"
        or entry_point != "autoform_cli.__main__:main"
    ):
        raise _GitFailure
    _require_remote_python_requirements(build_system.get("requires"))
    _require_remote_python_requirements(metadata.get("dependencies", []))
    optional_dependencies = metadata.get("optional-dependencies", {})
    if type(optional_dependencies) is not dict:
        raise _GitFailure
    for requirements in optional_dependencies.values():
        _require_remote_python_requirements(requirements)
    dynamic = metadata.get("dynamic", [])
    if dynamic != []:
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


def _require_https_lock_url(value: object) -> None:
    if (
        type(value) is not str
        or value != value.strip()
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise _GitFailure
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise _GitFailure from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise _GitFailure


def _validate_uv_lock(encoded: bytes) -> None:
    try:
        lock = loads_bounded_toml(
            encoded.decode("utf-8", errors="strict"),
            max_depth=_MAX_TOML_DEPTH,
        )
        packages = lock["package"]
    except (BoundedTomlError, KeyError, TypeError, UnicodeDecodeError) as error:
        raise _GitFailure from error
    if type(packages) is not list or len(packages) > _MAX_MANIFEST_ENTRIES:
        raise _GitFailure
    autoform_packages = 0
    for package in packages:
        if type(package) is not dict or type(package.get("name")) is not str:
            raise _GitFailure
        source = package.get("source")
        if package["name"] == "autoform":
            if source != {"editable": "."}:
                raise _GitFailure
            autoform_packages += 1
        else:
            if type(source) is not dict or set(source) != {"registry"}:
                raise _GitFailure
            _require_https_lock_url(source["registry"])
        artifacts: list[object] = []
        if "sdist" in package:
            artifacts.append(package["sdist"])
        wheels = package.get("wheels", [])
        if type(wheels) is not list:
            raise _GitFailure
        artifacts.extend(wheels)
        if package["name"] != "autoform" and not artifacts:
            raise _GitFailure
        for artifact in artifacts:
            if (
                type(artifact) is not dict
                or not {"url", "hash", "size"}.issubset(artifact)
                or not set(artifact).issubset({"url", "hash", "size", "upload-time"})
                or type(artifact["hash"]) is not str
                or _LOCK_HASH.fullmatch(artifact["hash"]) is None
                or type(artifact["size"]) is not int
                or artifact["size"] <= 0
            ):
                raise _GitFailure
            _require_https_lock_url(artifact["url"])
            upload_time = artifact.get("upload-time")
            if upload_time is not None and (
                type(upload_time) is not str
                or not upload_time
                or upload_time != upload_time.strip()
                or any(ord(character) < 32 or ord(character) == 127 for character in upload_time)
            ):
                raise _GitFailure
    if autoform_packages != 1:
        raise _GitFailure


def _validate_package_manifest(encoded: bytes) -> None:
    try:
        package = json.loads(
            encoded.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
        )
    except (
        UnicodeDecodeError,
        ValueError,
        RecursionError,
        MemoryError,
        _InvalidJson,
    ) as error:
        raise _GitFailure from error
    if not isinstance(package, dict) or "workspaces" in package:
        raise _GitFailure

    def require_remote_specifications(value: object) -> None:
        pending = [(value, 0)]
        while pending:
            current, depth = pending.pop()
            if not isinstance(current, dict) or depth > _MAX_JSON_DEPTH:
                raise _GitFailure
            for specification in current.values():
                if isinstance(specification, dict):
                    pending.append((specification, depth + 1))
                    continue
                if not isinstance(specification, str) or specification != specification.strip():
                    raise _GitFailure
                if specification in {".", ".."} or specification.casefold().startswith(
                    ("file:", "git+file:", "link:", "workspace:", "./", "../", "/", "~/")
                ):
                    raise _GitFailure

    for dependency_field in (
        "dependencies",
        "devDependencies",
        "optionalDependencies",
        "peerDependencies",
        "overrides",
        "resolutions",
    ):
        require_remote_specifications(package.get(dependency_field, {}))


def _under_root(relative: str, root: str) -> bool:
    return relative == root or relative.startswith(f"{root}/")


def _optional_shipped_root(relative: str) -> str | None:
    folded = relative.casefold()
    return next(
        (
            root
            for root in _OPTIONAL_SHIPPED_ROOTS
            if folded == root or folded.startswith(f"{root}/")
        ),
        None,
    )


def _optional_shipped_file(relative: str) -> str | None:
    folded = relative.casefold()
    return next(
        (candidate for candidate in _OPTIONAL_SHIPPED_FILES if folded == candidate),
        None,
    )


def _require_canonical_optional_surfaces(paths: Iterable[str]) -> None:
    for relative in paths:
        parts = PurePosixPath(relative).parts
        if parts and parts[0].casefold() in _FORBIDDEN_RUNTIME_ROOTS:
            raise _GitFailure
        if "/" not in relative and relative.casefold() == _CLAUDE_BUILD_COMMIT.casefold():
            raise _GitFailure
        if "/" not in relative and relative.casefold() in _FORBIDDEN_RUNTIME_FILES:
            raise _GitFailure
        root = _optional_shipped_root(relative)
        if root is not None and not _under_root(relative, root):
            raise _GitFailure
        expected_file = _optional_shipped_file(relative)
        if expected_file is not None and relative != expected_file:
            raise _GitFailure


def _source_roots(paths: Iterable[str], base_roots: Iterable[str]) -> tuple[str, ...]:
    roots = set(base_roots)
    for relative in paths:
        parts = PurePosixPath(relative).parts
        if len(parts) == 1 and _looks_importable(relative):
            raise _GitFailure
        if len(parts) > 1:
            roots.add(parts[0])
    return tuple(sorted(roots))


def _fetch_source_layout(source: str, revision: str, scratch: Path) -> _SourceLayout:
    deadline = time.monotonic() + 60
    repository = scratch / "repository.git"
    _run_git(["init", "--bare", "--template=", str(repository)], cwd=scratch, deadline=deadline)
    _run_git(["remote", "add", "origin", source], cwd=repository, deadline=deadline)
    _run_git(["config", "remote.origin.promisor", "true"], cwd=repository, deadline=deadline)
    _run_git(
        ["config", "remote.origin.partialCloneFilter", "blob:none"],
        cwd=repository,
        deadline=deadline,
    )
    _run_git(
        [
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--depth=1",
            "--filter=blob:none",
            "origin",
            revision,
        ],
        cwd=repository,
        deadline=deadline,
        max_stdout_bytes=1024 * 1024,
    )
    resolved = _git_text(
        ["rev-parse", "--verify", "FETCH_HEAD^{commit}"], cwd=repository, deadline=deadline
    ).lower()
    if resolved != revision:
        raise _GitFailure
    listing = _run_git(
        ["ls-tree", "-rz", "--full-tree", resolved],
        cwd=repository,
        deadline=deadline,
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
        if _FULL_SHA.fullmatch(object_id) is None:
            raise _GitFailure
        relative = _valid_relative_path(raw_path)
        if relative in objects:
            raise _GitFailure
        objects[relative] = _TreeObject(mode=mode, kind=kind, object_id=object_id)

    _require_canonical_optional_surfaces(objects)

    # Git has no stable porcelain for querying a promised blob's remote size.
    # Require the server to honor blob:none, then request only objects inside
    # the verified install boundary. Size limits are enforced as those selected
    # objects leave cat-file; they cannot be preflighted before their transfer.
    _require_blob_presence(repository, objects.values(), set(), deadline=deadline)

    pyproject_object = objects.get("pyproject.toml")
    if pyproject_object is None or pyproject_object.kind != "blob" or pyproject_object.mode != 0o100644:
        raise _GitFailure
    uv_lock_object = objects.get("uv.lock")
    if uv_lock_object is None or uv_lock_object.kind != "blob" or uv_lock_object.mode != 0o100644:
        raise _GitFailure
    metadata_objects = [pyproject_object, uv_lock_object]
    package_object = objects.get("package.json")
    if package_object is not None:
        if package_object.kind != "blob" or package_object.mode != 0o100644:
            raise _GitFailure
        metadata_objects.append(package_object)
    metadata_ids = {entry.object_id for entry in metadata_objects}
    _fetch_git_objects(repository, metadata_ids, deadline=deadline)
    _require_blob_presence(repository, objects.values(), metadata_ids, deadline=deadline)
    blob_contents = _read_git_blobs(repository, metadata_objects, deadline=deadline)
    pyproject = blob_contents[pyproject_object.object_id]
    package_roots = _package_roots(pyproject)
    _validate_uv_lock(blob_contents[uv_lock_object.object_id])
    if package_object is not None:
        _validate_package_manifest(blob_contents[package_object.object_id])
    optional_roots = {
        root
        for root in _OPTIONAL_SHIPPED_ROOTS
        if any(_under_root(path, root) for path in objects)
    }
    roots = _source_roots(
        objects,
        (*_SHIPPED_ROOTS, *optional_roots, *package_roots),
    )
    for root in roots:
        if not any(_under_root(path, root) for path in objects):
            raise _GitFailure
    if not _SHIPPED_FILES.issubset(objects):
        raise _GitFailure

    selected: dict[str, _TreeObject] = {}
    for relative, tree_object in sorted(objects.items()):
        path = PurePosixPath(relative)
        if any(part.casefold() == "__pycache__" for part in path.parts) or path.suffix.casefold() in {
            ".pyc",
            ".pyo",
        }:
            raise _GitFailure
        if tree_object.kind != "blob" or tree_object.mode not in {0o100644, 0o100755}:
            raise _GitFailure
        selected[relative] = tree_object

    selected_ids = {entry.object_id for entry in selected.values()}
    _fetch_git_objects(repository, selected_ids - blob_contents.keys(), deadline=deadline)
    _require_blob_presence(repository, objects.values(), selected_ids, deadline=deadline)
    blob_contents = _read_git_blobs(
        repository,
        selected.values(),
        deadline=deadline,
        known=blob_contents,
    )

    manifest: dict[str, _ManifestEntry] = {}
    total = 0
    for relative, tree_object in selected.items():
        content = blob_contents[tree_object.object_id]
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
        tag = match.group("tag")
        if tag.casefold() == current_tag.casefold() and tag != current_tag:
            raise ProvenanceError("The installed bytecode cache is invalid.")
        if tag != current_tag:
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


def _derived_entry_kind(relative: str) -> str | None:
    path = PurePosixPath(relative)
    if len(path.parts) != 1:
        return None
    if relative == INSTALL_RECORD:
        return "file"
    if relative == ".git":
        return "git"
    if relative in _DERIVED_ROOTS or relative in _DERIVED_DIRECTORY_NAMES:
        return "directory"
    if relative in _DERIVED_FILE_NAMES:
        return "file"
    return None


def _require_derived_entry(
    descriptor: int,
    name: str,
    relative: str,
) -> bool:
    kind = _derived_entry_kind(relative)
    if kind is None:
        return False
    try:
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except OSError as error:
        raise ProvenanceError("The installed Autoform derived state is invalid.") from error
    regular_non_executable = stat.S_ISREG(metadata.st_mode) and not metadata.st_mode & 0o111
    if kind == "directory" and not stat.S_ISDIR(metadata.st_mode):
        raise ProvenanceError("The installed Autoform derived state is invalid.")
    if kind == "file" and not regular_non_executable:
        raise ProvenanceError("The installed Autoform derived state is invalid.")
    if kind == "git" and not (stat.S_ISDIR(metadata.st_mode) or regular_non_executable):
        raise ProvenanceError("The installed Autoform derived state is invalid.")
    return True


def _looks_importable(relative: str) -> bool:
    name = PurePosixPath(relative).name.casefold()
    return any(name.endswith(suffix) for suffix in _IMPORTABLE_SUFFIXES)


def _scan_for_unverified_entries(
    descriptor: int,
    prefix: str,
    *,
    layout: _SourceLayout,
    allowed_host_files: frozenset[str],
    counter: list[int],
    depth: int,
) -> None:
    if depth > _MAX_PATH_DEPTH:
        raise ProvenanceError("The installed Autoform tree is too deep to verify safely.")
    for name in _safe_names(descriptor, counter):
        relative = f"{prefix}/{name}" if prefix else name
        if "/" not in relative and relative.casefold() in _FORBIDDEN_RUNTIME_FILES:
            raise ProvenanceError(
                "The installed Autoform tree contains unverified runtime configuration."
            )
        if any(_under_root(relative, root) for root in layout.roots):
            continue
        if relative in layout.files:
            continue
        if relative in allowed_host_files:
            continue
        if (expected_file := _optional_shipped_file(relative)) is not None:
            if relative == expected_file and relative in layout.files:
                continue
            raise ProvenanceError(
                "The installed Autoform tree contains an unverified plugin file."
            )
        if _optional_shipped_root(relative) is not None:
            raise ProvenanceError(
                "The installed Autoform tree contains an unverified plugin surface."
            )
        if _require_derived_entry(descriptor, name, relative):
            continue
        raise ProvenanceError(
            "The installed Autoform tree contains an unverified file or directory."
        )


def _json_type_exact(left: object, right: object) -> bool:
    pending: list[tuple[object, object, int]] = [(left, right, 1)]
    while pending:
        left_value, right_value, depth = pending.pop()
        if type(left_value) is not type(right_value) or depth > _MAX_JSON_DEPTH:
            return False
        if isinstance(left_value, dict):
            assert isinstance(right_value, dict)
            if set(left_value) != set(right_value):
                return False
            pending.extend(
                (left_value[key], right_value[key], depth + 1) for key in left_value
            )
        elif isinstance(left_value, list):
            assert isinstance(right_value, list)
            if len(left_value) != len(right_value):
                return False
            pending.extend(
                (left_item, right_item, depth + 1)
                for left_item, right_item in zip(left_value, right_value, strict=True)
            )
        elif left_value != right_value:
            return False
    return True


def _validate_claude_overlay(
    root_descriptor: int,
    layout: _SourceLayout,
    actual_files: dict[str, _ActualEntry],
    candidate: _Candidate,
    budget: list[int],
) -> frozenset[str]:
    message = "The installed Claude plugin metadata is invalid."
    if candidate.installed_version is None or candidate.cache_version is None:
        raise ProvenanceError(message)
    if candidate.registry_revision != candidate.revision:
        raise ProvenanceError(message)
    expected_objects: dict[str, dict[str, Any]] = {}
    actual_objects: dict[str, dict[str, Any]] = {}
    base_versions: set[str] = set()
    for relative in _CLAUDE_REWRITTEN_MANIFESTS:
        expected = layout.files.get(relative)
        actual = actual_files.get(relative)
        if expected is None or actual is None:
            raise ProvenanceError(message)
        expected_object = _decode_json_object(expected.content, message)
        actual_object = _decode_json_object(actual.content, message)
        base_version = expected_object.get("version")
        if type(base_version) is not str or _CLAUDE_BASE_VERSION.fullmatch(base_version) is None:
            raise ProvenanceError(message)
        base_versions.add(base_version)
        expected_objects[relative] = expected_object
        actual_objects[relative] = actual_object
    if len(base_versions) != 1:
        raise ProvenanceError(message)
    base_version = next(iter(base_versions))
    installed_version = f"{base_version}+deicyde.{candidate.revision[:7]}"
    if (
        candidate.installed_version != installed_version
        or candidate.cache_version != installed_version.replace("+", "-", 1)
    ):
        raise ProvenanceError(message)
    for relative in _CLAUDE_REWRITTEN_MANIFESTS:
        expected_object = expected_objects[relative]
        expected_object["version"] = installed_version
        if not _json_type_exact(expected_object, actual_objects[relative]):
            raise ProvenanceError(message)
    read = _read_bounded_regular(
        root_descriptor,
        _CLAUDE_BUILD_COMMIT,
        limit=41,
        message=message,
    )
    if read is None:
        raise ProvenanceError(message)
    content, metadata = read
    if content != f"{candidate.revision}\n".encode("ascii") or metadata.st_mode & 0o111:
        raise ProvenanceError(message)
    budget[0] += len(content)
    if budget[0] > _MAX_SHIPPED_TOTAL_BYTES:
        raise ProvenanceError("The installed Autoform tree is too large to verify safely.")
    return frozenset({_CLAUDE_BUILD_COMMIT})


def _compare_installed_tree(
    root: Path,
    root_descriptor: int,
    layout: _SourceLayout,
    candidate: _Candidate,
) -> None:
    actual_files: dict[str, _ActualEntry] = {}
    actual_directories: set[str] = set()
    bytecode: list[_CachedBytecode] = []
    counter = [0]
    budget = [0]

    roots: list[str] = []
    for root_candidate in sorted(
        layout.roots,
        key=lambda value: (len(PurePosixPath(value).parts), value),
    ):
        if not any(_under_root(root_candidate, selected) for selected in roots):
            roots.append(root_candidate)
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

    root_files = {
        relative for relative in layout.files if len(PurePosixPath(relative).parts) == 1
    }
    for relative in root_files:
        if len(PurePosixPath(relative).parts) != 1:
            raise ProvenanceError("The installed Autoform boundary is invalid.")
        actual_files[relative] = _read_actual_file(root_descriptor, relative, budget)

    expected_directories = _expected_directories(layout.files)
    if set(actual_files) != set(layout.files) or actual_directories != expected_directories:
        raise ProvenanceError("The installed Autoform tree does not match the recorded commit.")
    mismatched: set[str] = set()
    for relative, expected in layout.files.items():
        found = actual_files[relative]
        if found.mode != expected.mode:
            raise ProvenanceError("The installed Autoform files do not match the recorded commit.")
        if found.content != expected.content:
            mismatched.add(relative)
    if mismatched:
        if mismatched != _CLAUDE_REWRITTEN_MANIFESTS:
            raise ProvenanceError("The installed Autoform files do not match the recorded commit.")
        allowed_host_files = _validate_claude_overlay(
            root_descriptor,
            layout,
            actual_files,
            candidate,
            budget,
        )
    else:
        allowed_host_files = frozenset()
    _validate_bytecode(root, bytecode, actual_files, layout.files)
    _scan_for_unverified_entries(
        root_descriptor,
        "",
        layout=layout,
        allowed_host_files=allowed_host_files,
        counter=[0],
        depth=0,
    )


def _verify_plugin_layout(
    root: Path | None = None,
) -> tuple[PluginProvenance, _SourceLayout]:
    """Return provenance together with the exact verified source snapshot."""

    selected_root, root_descriptor = _open_root(root or plugin_root())
    try:
        checkout = _checkout_candidate(selected_root, root_descriptor)
        record = _read_install_record(root_descriptor)
        claude = (
            None
            if checkout is not None or record is not None
            else _claude_marketplace_candidate(selected_root)
        )
        candidates = [item for item in (checkout, record, claude) if item is not None]
        if len(set(candidates)) > 1:
            raise ProvenanceError("The Autoform provenance records conflict.")
        if not candidates:
            raise ProvenanceError("No trustworthy Autoform source and commit are available.")
        candidate = candidates[0]
        _require_root_identity(selected_root, root_descriptor)
        try:
            with tempfile.TemporaryDirectory(prefix="autoform-provenance-") as temporary:
                layout = _fetch_source_layout(
                    candidate.source,
                    candidate.revision,
                    Path(temporary),
                )
                _compare_installed_tree(selected_root, root_descriptor, layout, candidate)
                # Re-read the complete boundary before committing the result.
                # A mutation after an earlier root was scanned must not be
                # hidden by that root's unchanged parent-directory identity.
                _compare_installed_tree(selected_root, root_descriptor, layout, candidate)
        except ProvenanceError:
            raise
        except (_GitFailure, OSError) as error:
            raise ProvenanceError("The recorded Autoform commit could not be verified.") from error
        _require_root_identity(selected_root, root_descriptor)
        return PluginProvenance(source=candidate.source, revision=candidate.revision), layout
    finally:
        os.close(root_descriptor)


def verify_plugin_provenance(root: Path | None = None) -> PluginProvenance:
    """Verify the source, remote commit, and installed plugin before returning."""

    verified, _ = _verify_plugin_layout(root)
    return verified


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
