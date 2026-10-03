"""Resolve the immutable source revision recorded for this Autoform install.

This module identifies a build; it does not try to make running code attest to
its own integrity. A checkout records its identity in Git, while plugin hosts
record it in their installation metadata. If several records are present they
must agree exactly.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


INSTALL_RECORD = ".codex-marketplace-install.json"
PROVENANCE_RECORD = ".autoform-provenance.json"
MAX_INSTALL_RECORD_BYTES = 64 * 1024
MAX_HOST_REGISTRY_BYTES = 1024 * 1024

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_SOURCE_HOST = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
)
_SOURCE_PATH_PART = re.compile(r"[A-Za-z0-9._~-]+")
_GITHUB_SCP_SOURCE = re.compile(
    r"git@github\.com:(?P<path>[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)+)"
)
_PLUGIN_ROOT = Path(os.path.abspath(Path(__file__).parent.parent))


class ProvenanceError(ValueError):
    """No unambiguous immutable source revision is recorded for Autoform."""

    code = "project-provenance-unavailable"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PluginProvenance:
    """A credential-free Git source and its recorded full commit SHA."""

    source: str
    revision: str

    def as_dict(self) -> dict[str, object]:
        return {"ok": True, "revision": self.revision, "source": self.source}


def plugin_root() -> Path:
    """Return the source root containing the running ``autoform_cli``."""

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


def _signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_regular(path: Path, *, limit: int, message: str) -> bytes | None:
    """Read one bounded regular file without accepting a final-component link."""

    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ProvenanceError(message) from error
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ProvenanceError(message)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if _signature(opened) != _signature(before):
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
        if (
            len(content) > limit
            or len(content) != opened.st_size
            or _signature(os.fstat(descriptor)) != _signature(opened)
            or _signature(path.lstat()) != _signature(opened)
        ):
            raise ProvenanceError(message)
        return content
    except ProvenanceError:
        raise
    except OSError as error:
        raise ProvenanceError(message) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


class _DuplicateJsonKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _read_json(path: Path, *, limit: int, message: str) -> dict[str, Any] | None:
    encoded = _read_regular(path, limit=limit, message=message)
    if encoded is None:
        return None
    try:
        payload = json.loads(
            encoded.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, ValueError, RecursionError, MemoryError) as error:
        raise ProvenanceError(message) from error
    if not isinstance(payload, dict):
        raise ProvenanceError(message)
    return payload


def _candidate(source: object, revision: object, message: str) -> PluginProvenance:
    if type(source) is not str or type(revision) is not str:
        raise ProvenanceError(message)
    normalized_source = normalize_git_source(
        source,
        allow_github_scp=True,
        add_git_suffix=True,
    )
    normalized_revision = revision.lower()
    if normalized_source is None or _FULL_SHA.fullmatch(normalized_revision) is None:
        raise ProvenanceError(message)
    return PluginProvenance(normalized_source, normalized_revision)


def _git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    return environment


def _git(
    root: Path,
    arguments: list[str],
    *,
    accepted: frozenset[int] = frozenset({0}),
) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "credential.helper=",
                *arguments,
            ],
            cwd=root,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise ProvenanceError("The Autoform checkout metadata is invalid.") from error
    if completed.returncode not in accepted or len(completed.stdout) > 16 * 1024:
        raise ProvenanceError("The Autoform checkout metadata is invalid.")
    try:
        output = completed.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise ProvenanceError("The Autoform checkout metadata is invalid.") from error
    return completed.returncode, output


def _checkout_source(root: Path) -> str:
    _, top = _git(root, ["rev-parse", "--show-toplevel"])
    try:
        if Path(top).resolve() != root.resolve():
            raise ProvenanceError("The Autoform checkout is not rooted at the plugin root.")
    except OSError as error:
        raise ProvenanceError("The Autoform checkout metadata is invalid.") from error
    _, source = _git(root, ["config", "--local", "--get", "remote.origin.url"])
    normalized = normalize_git_source(source, allow_github_scp=True, add_git_suffix=True)
    if normalized is None:
        raise ProvenanceError("The Autoform checkout metadata is invalid.")
    return normalized


def _checkout_candidate(root: Path) -> PluginProvenance | None:
    if not (root / ".git").exists():
        return None
    source = _checkout_source(root)
    _, revision = _git(root, ["rev-parse", "--verify", "HEAD^{commit}"])
    for arguments in (
        ["diff", "--quiet", "--no-ext-diff", "HEAD", "--"],
        ["diff", "--cached", "--quiet", "--no-ext-diff", "HEAD", "--"],
    ):
        returncode, _ = _git(root, arguments, accepted=frozenset({0, 1}))
        if returncode != 0:
            raise ProvenanceError(
                "The Autoform checkout has tracked changes; supply an explicit source and commit."
            )
    return _candidate(source, revision, "The Autoform checkout metadata is invalid.")


def _sidecar_candidate(root: Path) -> PluginProvenance | None:
    message = "The Autoform provenance record is invalid."
    payload = _read_json(root / PROVENANCE_RECORD, limit=MAX_INSTALL_RECORD_BYTES, message=message)
    if payload is None:
        return None
    return _candidate(payload.get("source"), payload.get("revision"), message)


def _codex_candidate(root: Path) -> PluginProvenance | None:
    message = "The Autoform Codex installation record is invalid."
    payload = _read_json(root / INSTALL_RECORD, limit=MAX_INSTALL_RECORD_BYTES, message=message)
    if payload is None:
        return None
    if payload.get("source_type") != "git":
        raise ProvenanceError(message)
    return _candidate(payload.get("source"), payload.get("revision"), message)


def _claude_registry_paths() -> tuple[Path, Path]:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(configured).expanduser() if configured else Path.home() / ".claude"
    plugins = Path(os.path.abspath(base)) / "plugins"
    return plugins / "installed_plugins.json", plugins / "known_marketplaces.json"


def _absolute_path(value: object, message: str) -> Path:
    if type(value) is not str or not value or len(value) > 4096:
        raise ProvenanceError(message)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ProvenanceError(message)
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ProvenanceError(message)
    return Path(os.path.abspath(path))


def _claude_coordinates(root: Path, registry: Path) -> tuple[str, str] | None:
    cache = Path(os.path.abspath(registry.parent / "cache"))
    try:
        relative = Path(os.path.abspath(root)).relative_to(cache)
    except ValueError:
        return None
    if len(relative.parts) != 3 or relative.parts[1] != "autoform":
        return None
    marketplace, _, _ = relative.parts
    if not marketplace or len(marketplace) > 255:
        raise ProvenanceError("The Claude plugin cache path is invalid.")
    return marketplace, "autoform"


def _claude_revision(root: Path, marketplace: str, plugin: str, registry: Path) -> str:
    message = "The Claude plugin installation record is invalid."
    payload = _read_json(registry, limit=MAX_HOST_REGISTRY_BYTES, message=message)
    if payload is None or not isinstance(payload.get("plugins"), dict):
        raise ProvenanceError(message)
    entries = payload["plugins"].get(f"{plugin}@{marketplace}")
    if not isinstance(entries, list):
        raise ProvenanceError(message)
    revisions: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ProvenanceError(message)
        if _absolute_path(entry.get("installPath"), message) != Path(os.path.abspath(root)):
            continue
        revision = entry.get("gitCommitSha")
        if type(revision) is not str or _FULL_SHA.fullmatch(revision.lower()) is None:
            raise ProvenanceError(message)
        revisions.append(revision.lower())
    if len(revisions) != 1:
        raise ProvenanceError(message)
    return revisions[0]


def _claude_source(marketplace: str, registry: Path) -> str:
    message = "The Claude plugin marketplace record is invalid."
    payload = _read_json(registry, limit=MAX_HOST_REGISTRY_BYTES, message=message)
    if payload is None or not isinstance(payload.get(marketplace), dict):
        raise ProvenanceError(message)
    checkout = _absolute_path(payload[marketplace].get("installLocation"), message)
    try:
        return _checkout_source(checkout)
    except ProvenanceError as error:
        raise ProvenanceError(message) from error


def _claude_candidate(root: Path) -> PluginProvenance | None:
    installed_registry, marketplace_registry = _claude_registry_paths()
    coordinates = _claude_coordinates(root, installed_registry)
    if coordinates is None:
        return None
    marketplace, plugin = coordinates
    revision = _claude_revision(root, marketplace, plugin, installed_registry)
    source = _claude_source(marketplace, marketplace_registry)
    build_commit = _read_regular(
        root / "BUILD_COMMIT",
        limit=41,
        message="The Claude plugin build record is invalid.",
    )
    if build_commit is not None and build_commit != f"{revision}\n".encode("ascii"):
        raise ProvenanceError("The Claude plugin build record conflicts with its installation record.")
    return PluginProvenance(source, revision)


def resolve_plugin_provenance(root: Path | None = None) -> PluginProvenance:
    """Resolve one agreed source/revision identity from local host records."""

    selected = Path(os.path.abspath((root or plugin_root()).expanduser()))
    if selected.is_symlink() or not selected.is_dir():
        raise ProvenanceError("The Autoform plugin root is invalid.")
    candidates = [
        candidate
        for candidate in (
            _checkout_candidate(selected),
            _sidecar_candidate(selected),
            _codex_candidate(selected),
            _claude_candidate(selected),
        )
        if candidate is not None
    ]
    if not candidates:
        raise ProvenanceError("No immutable Autoform source and commit are recorded.")
    if any(candidate != candidates[0] for candidate in candidates[1:]):
        raise ProvenanceError("The Autoform provenance records conflict.")
    return candidates[0]


def plugin_pin() -> tuple[str, str]:
    """Return recorded all-or-nothing provenance for legacy callers."""

    try:
        provenance = resolve_plugin_provenance()
    except ProvenanceError:
        return "", ""
    return provenance.source, provenance.revision


__all__ = [
    "INSTALL_RECORD",
    "MAX_INSTALL_RECORD_BYTES",
    "PROVENANCE_RECORD",
    "PluginProvenance",
    "ProvenanceError",
    "normalize_git_source",
    "plugin_pin",
    "plugin_root",
    "resolve_plugin_provenance",
]
