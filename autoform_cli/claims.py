"""Host-neutral, fail-closed Git-ref leases for cooperative work claims.

Each claim is stored under ``refs/autoform-claims/`` and points to an orphan
commit whose message is the lease JSON. Mutations use an exact observed object
ID as a compare-and-swap precondition, so concurrent claimants cannot silently
overwrite one another.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shlex
import socket
import stat
import subprocess
import sys
import threading
import time
import weakref
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit
from urllib.request import url2pathname

CLAIM_REF_PREFIX = "refs/autoform-claims/"
CLAIM_RECEIPT_REF_PREFIX = "refs/autoform-claim-receipts/"
CLAIM_SCHEMA = "autoform-claim/v2"
LEGACY_CLAIM_SCHEMA = "autoform-claim/v1"
LEGACY_BLOCK_SCHEMA = "autoform-claim/legacy-block/v1"
_PERMANENT_BLOCK_SCHEMAS = frozenset({LEGACY_BLOCK_SCHEMA})
CLAIM_TTL_S = 1500
CLAIM_HEARTBEAT_S = 300
CLAIM_MAX_TTL_S = 3600
CLAIM_CLOCK_SKEW_S = 300
CLAIM_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
LEASE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
OBJECT_ID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_OBJECT_FORMAT_LENGTHS = {"sha1": 40, "sha256": 64}

_SCP_REPOSITORY_RE = re.compile(
    r"^(?:[^/@:]+@)?(?:\[[^\]]+\]|[^/:]+):.+$"
)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "autoform",
    "GIT_AUTHOR_EMAIL": "autoform@localhost",
    "GIT_COMMITTER_NAME": "autoform",
    "GIT_COMMITTER_EMAIL": "autoform@localhost",
    "GIT_CONFIG_COUNT": "0",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
}
_GIT_ENV_ALLOWLIST = frozenset(
    {
        "ALL_PROXY",
        "COMSPEC",
        "CURL_CA_BUNDLE",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LANGUAGE",
        "LOGNAME",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SSH_AUTH_SOCK",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "WINDIR",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_CAS_REJECTIONS = (
    "stale info",
    "fetch first",
    "remote ref updated since checkout",
    "cannot lock ref",
)
_UNPINNED_REPOSITORY = object()
_UNPINNED_SCRATCH = object()
_FCHDIR_EXEC = (
    "import os,sys; os.fchdir(int(sys.argv[1])); "
    "os.execvp(sys.argv[2], sys.argv[2:])"
)


class ClaimTransportError(RuntimeError):
    """A claim board operation could not be completed or verified."""


class MalformedLeaseError(ClaimTransportError):
    """A claim ref exists, but its lease cannot be verified safely."""


@dataclass(frozen=True, slots=True)
class ClaimFence:
    """One coherent, exact remote ownership receipt for an acquired claim."""

    key: str
    ref: str
    oid: str
    lease_id: str

    def __post_init__(self) -> None:
        key = _validate_key(self.key)
        if self.ref != CLAIM_REF_PREFIX + key:
            raise ValueError("claim fence ref does not match its key")
        if not isinstance(self.oid, str) or OBJECT_ID_RE.fullmatch(self.oid) is None:
            raise ValueError("claim fence OID must be a full Git object ID")
        if set(self.oid) == {"0"}:
            raise ValueError("claim fence OID must identify an object")
        if not isinstance(self.lease_id, str) or LEASE_ID_RE.fullmatch(self.lease_id) is None:
            raise ValueError("claim fence lease_id must be 64 lowercase hexadecimal characters")

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _validate_key(key: str) -> str:
    if not isinstance(key, str) or not CLAIM_KEY_RE.fullmatch(key) or ".." in key:
        raise ValueError(f"invalid claim key {key!r}")
    parts = key.split("/")
    if any(part.startswith(".") or part.endswith(".") or part.endswith(".lock") for part in parts):
        raise ValueError(f"invalid claim key {key!r}")
    return key


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _validate_ttl(ttl: int | float) -> int | float:
    if not _is_finite_number(ttl) or ttl <= 0:
        raise ValueError("claim TTL must be a finite positive number")
    if ttl > CLAIM_MAX_TTL_S:
        raise ValueError(f"claim TTL must not exceed {CLAIM_MAX_TTL_S} seconds")
    return ttl


def _validate_object_format(value: str) -> str:
    if not isinstance(value, str) or value not in _OBJECT_FORMAT_LENGTHS:
        choices = ", ".join(sorted(_OBJECT_FORMAT_LENGTHS))
        raise ValueError(f"Git object format must be one of: {choices}")
    return value


def _canonical_scratch_config(object_format: str) -> bytes:
    object_format = _validate_object_format(object_format)
    version = 0 if object_format == "sha1" else 1
    extension = "" if object_format == "sha1" else "[extensions]\n\tobjectFormat = sha256\n"
    return (
        "[core]\n"
        f"\trepositoryformatversion = {version}\n"
        "\tbare = true\n"
        f"\thooksPath = {os.devnull}\n"
        f"{extension}"
    ).encode()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _resolve_local_path(value: str | os.PathLike[str], *, label: str) -> Path:
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} path cannot be resolved safely") from exc


def _directory_identity(
    path: Path,
    *,
    label: str,
    allow_missing: bool,
) -> tuple[int, int] | None:
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise ClaimTransportError(f"{label} directory is no longer available") from None
    except OSError as exc:
        raise ClaimTransportError(f"{label} directory cannot be inspected safely") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ClaimTransportError(f"{label} path must be a real directory")
    return info.st_dev, info.st_ino


def _directory_path_snapshot(
    path: Path,
    *,
    anchor: Path,
    label: str,
) -> tuple[tuple[int, int, int | None], ...]:
    try:
        relative = path.relative_to(anchor)
    except ValueError as exc:
        raise ClaimTransportError(f"{label} escaped its pinned filesystem boundary") from exc
    components = [anchor]
    for part in relative.parts:
        components.append(components[-1] / part)
    snapshot: list[tuple[int, int, int | None]] = []
    for component in components:
        try:
            info = component.stat(follow_symlinks=False)
        except OSError as exc:
            raise ClaimTransportError(
                f"{label} path component cannot be inspected safely"
            ) from exc
        if not stat.S_ISDIR(info.st_mode):
            raise ClaimTransportError(f"{label} path component must be a real directory")
        changed_at_ns = None if component == path else info.st_ctime_ns
        snapshot.append((info.st_dev, info.st_ino, changed_at_ns))
    return tuple(snapshot)


def _directory_operation_guard(
    path: Path,
    *,
    anchor: Path,
    label: str,
) -> tuple[tuple[int, int, int | None], ...]:
    """Stably capture every path component around one Git subprocess."""
    before = _directory_path_snapshot(path, anchor=anchor, label=label)
    after = _directory_path_snapshot(path, anchor=anchor, label=label)
    if before != after:
        raise ClaimTransportError(f"{label} changed while its path was being inspected")
    return after


def claim_repository_is_remote(repo_url: str | os.PathLike[str]) -> bool:
    """Return whether Git will treat this repository name as a remote transport."""
    value = os.fspath(repo_url)
    if _WINDOWS_DRIVE_RE.match(value):
        return False
    return "://" in value or bool(_SCP_REPOSITORY_RE.match(value))


def normalize_claim_repository(repo_url: str | os.PathLike[str]) -> str:
    """Return a stable transport identity, resolving local paths and file URLs."""
    raw_repo_url = os.fspath(repo_url)
    parsed = urlsplit(raw_repo_url)
    if parsed.scheme.lower() == "file":
        if parsed.query or parsed.fragment or parsed.netloc.lower() not in {"", "localhost"}:
            raise ValueError("file repository URL must identify an absolute local path")
        local_path = Path(url2pathname(parsed.path))
        if not local_path.is_absolute():
            raise ValueError("file repository URL must identify an absolute local path")
        return str(_resolve_local_path(local_path, label="claim repository"))
    if not claim_repository_is_remote(raw_repo_url):
        return str(_resolve_local_path(raw_repo_url, label="claim repository"))
    return raw_repo_url


def pin_claim_repository(
    repo_url: str | os.PathLike[str],
) -> tuple[str, tuple[int, int] | None]:
    """Resolve a claim repository and capture its local filesystem identity."""
    normalized = normalize_claim_repository(repo_url)
    local_path = None if claim_repository_is_remote(normalized) else Path(normalized)
    identity = (
        _directory_identity(
            local_path,
            label="local claim repository",
            allow_missing=True,
        )
        if local_path is not None
        else None
    )
    return normalized, identity


def pin_claim_scratch(
    scratch: str | os.PathLike[str],
) -> tuple[Path, tuple[int, int] | None]:
    """Resolve a scratch path and capture an existing directory's identity."""
    path = _resolve_local_path(scratch, label="claim scratch")
    return path, _directory_identity(
        path,
        label="claim scratch",
        allow_missing=True,
    )


def author_claim_key(node_id: str) -> str:
    """Return a readable, ref-safe, collision-resistant author claim key."""
    if not isinstance(node_id, str):
        raise TypeError("node_id must be a string")
    slug = re.sub(r"[^a-z0-9-]+", "-", node_id.lower()).strip("-")[:48] or "node"
    digest = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:16]
    return f"author/{slug}-{digest}"


def workspace_author_claim_key(project_id: str, article_id: str) -> str:
    """Return an article claim key scoped to one stable workspace project id."""

    if not isinstance(project_id, str) or not project_id:
        raise ValueError("workspace project id must not be empty")
    project = author_claim_key(project_id).removeprefix("author/")
    article = author_claim_key(article_id).removeprefix("author/")
    return f"author/workspace-{project}/{article}"


def resource_claim_key(resource: str) -> str:
    """Return a ref-safe key in the namespace for non-article resources."""
    if not isinstance(resource, str):
        raise TypeError("resource must be a string")
    if not resource:
        raise ValueError("resource must not be empty")
    slug = re.sub(r"[^a-z0-9-]+", "-", resource.lower()).strip("-")[:48] or "resource"
    digest = hashlib.sha256(resource.encode("utf-8")).hexdigest()[:16]
    return f"resource/{slug}-{digest}"


def _open_pinned_directory(
    path: Path,
    identity: tuple[int, int] | None,
    *,
    label: str,
) -> int | None:
    if identity is None or os.name != "posix" or not hasattr(os, "fchdir"):
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ClaimTransportError(f"{label} cannot be pinned safely") from exc
    if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != identity:
        os.close(descriptor)
        raise ClaimTransportError(f"{label} was replaced")
    return descriptor


def _claim_git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _GIT_ENV_ALLOWLIST or key.startswith("LC_")
    }
    environment.setdefault("PATH", os.defpath)
    environment.update(_GIT_ENV)
    return environment


def _parse_ls_remote_output(
    output: str,
    *,
    allow_head: bool = False,
) -> list[tuple[str, str]]:
    if not output:
        return []
    entries: list[tuple[str, str]] = []
    lines = output.split("\n")
    if lines[-1] == "":
        lines.pop()
    for line in lines:
        oid, separator, ref = line.partition("\t")
        if (
            not separator
            or not OBJECT_ID_RE.fullmatch(oid)
            or (not ref.startswith("refs/") and not (allow_head and ref == "HEAD"))
            or any(
                character == " " or ord(character) < 32 or ord(character) == 127
                for character in ref
            )
        ):
            raise ClaimTransportError("claim board returned malformed ls-remote output")
        entries.append((oid, ref))
    return entries


class ClaimBoard:
    """Lease operations against a Git repository via a local bare object store."""

    def __init__(
        self,
        repo_url: str | os.PathLike[str],
        worker_id: str,
        scratch: str | os.PathLike[str],
        *,
        session_id: str | None = None,
        expected_object_format: str | None = None,
        expected_repo_identity: object = _UNPINNED_REPOSITORY,
        expected_scratch_identity: object = _UNPINNED_SCRATCH,
    ):
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        validated_object_format = (
            _validate_object_format(expected_object_format)
            if expected_object_format is not None
            else None
        )
        self.repo_url, current_repo_identity = pin_claim_repository(repo_url)
        self._repo_path = (
            None if claim_repository_is_remote(self.repo_url) else Path(self.repo_url)
        )
        if (
            expected_repo_identity is not _UNPINNED_REPOSITORY
            and current_repo_identity != expected_repo_identity
        ):
            raise ClaimTransportError("local claim repository was replaced")
        self._repo_identity = current_repo_identity
        self.worker_id = worker_id
        self.scratch, current_scratch_identity = pin_claim_scratch(scratch)
        if (
            expected_scratch_identity is not _UNPINNED_SCRATCH
            and current_scratch_identity != expected_scratch_identity
        ):
            raise ClaimTransportError("claim scratch directory was replaced")
        self._scratch_identity = current_scratch_identity
        if self._scratch_identity is None:
            self.scratch.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.scratch.mkdir()
            except FileExistsError as exc:
                raise ClaimTransportError(
                    "claim scratch directory appeared while its identity was being pinned"
                ) from exc
            self._scratch_identity = _directory_identity(
                self.scratch,
                label="claim scratch",
                allow_missing=False,
            )
        self._path_anchor = (
            Path(os.path.commonpath((self._repo_path, self.scratch)))
            if self._repo_path is not None
            else self.scratch
        )
        self._scratch_fd = _open_pinned_directory(
            self.scratch,
            self._scratch_identity,
            label="claim scratch directory",
        )
        self._repo_fd = (
            _open_pinned_directory(
                self._repo_path,
                self._repo_identity,
                label="local claim repository",
            )
            if self._repo_path is not None
            else None
        )
        self._fd_finalizers: list[weakref.finalize] = []
        for descriptor in (self._scratch_fd, self._repo_fd):
            if descriptor is not None:
                self._fd_finalizers.append(weakref.finalize(self, os.close, descriptor))
        self._transport_helper = Path(__file__).with_name("_git_fd_transport.py").resolve()
        self._scratch_ready = False
        self._expected_object_format = validated_object_format
        self._object_format: str | None = None
        if session_id is None:
            session_id = f"scratch:{self.scratch}"
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must not be empty")
        self.session_id = session_id
        self._session_key = hashlib.sha256(
            f"{self.repo_url}\0{session_id}".encode("utf-8")
        ).hexdigest()

    def _git(
        self,
        args: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        remote: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        display_args = args
        if remote and self._repo_fd is not None:
            args = self._local_transport_args(args)
        self._verify_scratch_identity()
        scratch_guard = None
        repo_guard = None
        if self._scratch_fd is None:
            scratch_guard = _directory_operation_guard(
                self.scratch,
                anchor=self._path_anchor,
                label="claim scratch",
            )
        if remote:
            self._verify_repo_identity()
            if (
                self._repo_fd is None
                and self._repo_path is not None
                and self._repo_identity is not None
            ):
                repo_guard = _directory_operation_guard(
                    self._repo_path,
                    anchor=self._path_anchor,
                    label="local claim repository",
            )
        try:
            environment = {**_claim_git_environment(), "GIT_DIR": "."}
            command = ["git", *args]
            run_options: dict[str, Any] = {"cwd": self.scratch}
            descriptors = tuple(
                descriptor
                for descriptor in (self._scratch_fd, self._repo_fd if remote else None)
                if descriptor is not None
            )
            if self._scratch_fd is not None:
                command = [
                    sys.executable,
                    "-c",
                    _FCHDIR_EXEC,
                    str(self._scratch_fd),
                    "git",
                    *args,
                ]
                run_options = {"pass_fds": descriptors}
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                errors="surrogateescape",
                input=input_text,
                timeout=120,
                env=environment,
                **run_options,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClaimTransportError(f"git claim-board operation failed: {exc}") from exc
        self._verify_scratch_identity()
        if remote:
            self._verify_repo_identity()
            if repo_guard is not None and (
                _directory_operation_guard(
                    self._repo_path,
                    anchor=self._path_anchor,
                    label="local claim repository",
                )
                != repo_guard
            ):
                raise ClaimTransportError(
                    "local claim repository changed during a Git operation"
                )
        if scratch_guard is not None and (
            _directory_operation_guard(
                self.scratch,
                anchor=self._path_anchor,
                label="claim scratch",
            )
            != scratch_guard
        ):
            raise ClaimTransportError("claim scratch changed during a Git operation")
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise ClaimTransportError(
                f"git {' '.join(display_args[:2])} failed against claim board: {detail}"
            )
        return proc

    def _local_transport_args(self, args: list[str]) -> list[str]:
        if self._repo_fd is None or not args:
            return args
        operation = args[0]
        if operation in {"ls-remote", "fetch"}:
            mode = "upload"
            option = "--upload-pack"
        elif operation == "push":
            mode = "receive"
            option = "--receive-pack"
        else:
            raise ClaimTransportError(
                f"unsupported local claim transport operation {operation!r}"
            )
        helper = shlex.join(
            (
                sys.executable,
                os.fspath(self._transport_helper),
                mode,
                str(self._repo_fd),
            )
        )
        rewritten = ["." if arg == self.repo_url else arg for arg in args]
        if rewritten == args:
            raise ClaimTransportError("local claim transport target was not explicit")
        rewritten.insert(1, f"{option}={helper}")
        return rewritten

    def _verify_repo_identity(self) -> None:
        if self._repo_path is None:
            return
        current = _directory_identity(
            self._repo_path,
            label="local claim repository",
            allow_missing=True,
        )
        if current != self._repo_identity:
            raise ClaimTransportError("local claim repository was replaced")

    def _remote_git(
        self,
        args: list[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        self._verify_repo_identity()
        proc = self._git(args, check=check, remote=True)
        self._verify_repo_identity()
        return proc

    def _verify_scratch_identity(self) -> None:
        current = _directory_identity(
            self.scratch,
            label="claim scratch",
            allow_missing=True,
        )
        if current != self._scratch_identity:
            raise ClaimTransportError("claim scratch directory was replaced")

    def _repository_object_format(self) -> str | None:
        self._verify_repo_identity()
        if self._repo_path is not None:
            command = ["git", "rev-parse", "--show-object-format"]
            run_options: dict[str, Any] = {"cwd": self._repo_path}
            if self._repo_fd is not None:
                command = [
                    sys.executable,
                    "-c",
                    _FCHDIR_EXEC,
                    str(self._repo_fd),
                    "git",
                    "rev-parse",
                    "--show-object-format",
                ]
                run_options = {"pass_fds": (self._repo_fd,)}
            try:
                proc = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=_claim_git_environment(),
                    **run_options,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ClaimTransportError(
                    f"cannot inspect claim repository object format: {exc}"
                ) from exc
            self._verify_repo_identity()
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout).strip()[:300]
                raise ClaimTransportError(
                    f"cannot inspect claim repository object format: {detail}"
                )
            detected = proc.stdout.strip()
        else:
            entries: list[tuple[str, str]] = []
            commands = (
                (["git", "ls-remote", "--refs", self.repo_url], False),
                (["git", "ls-remote", self.repo_url, "HEAD"], True),
            )
            for command, allow_head in commands:
                try:
                    proc = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        errors="surrogateescape",
                        timeout=120,
                        env=_claim_git_environment(),
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise ClaimTransportError(
                        f"cannot inspect claim repository object format: {exc}"
                    ) from exc
                if proc.returncode != 0:
                    detail = (proc.stderr or proc.stdout).strip()[:300]
                    raise ClaimTransportError(
                        f"cannot inspect claim repository object format: {detail}"
                    )
                entries = _parse_ls_remote_output(proc.stdout, allow_head=allow_head)
                if entries:
                    break
            widths = {len(oid) for oid, _ref in entries}
            if not widths:
                return self._expected_object_format
            if len(widths) != 1:
                raise ClaimTransportError("claim repository returned mixed object formats")
            width = widths.pop()
            detected = next(
                name for name, length in _OBJECT_FORMAT_LENGTHS.items() if length == width
            )
        try:
            detected = _validate_object_format(detected)
        except ValueError as exc:
            raise ClaimTransportError("claim repository has an unsupported object format") from exc
        if (
            self._expected_object_format is not None
            and detected != self._expected_object_format
        ):
            raise ClaimTransportError(
                f"claim repository object format {detected!r} does not match expected "
                f"{self._expected_object_format!r}"
            )
        return detected

    def _scratch_object_format(self) -> str:
        proc = self._git(["rev-parse", "--show-object-format"], check=False)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise ClaimTransportError(
                f"cannot inspect claim scratch object format: {detail}"
            )
        try:
            return _validate_object_format(proc.stdout.strip())
        except ValueError as exc:
            raise ClaimTransportError("claim scratch has an unsupported object format") from exc

    def _install_canonical_scratch_config(self, object_format: str) -> None:
        canonical_config = _canonical_scratch_config(object_format)
        read_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            read_flags |= os.O_NOFOLLOW
        existing: int | None = None
        try:
            if self._scratch_fd is not None:
                existing = os.open("config", read_flags, dir_fd=self._scratch_fd)
            else:
                existing = os.open(self.scratch / "config", read_flags)
            info = os.fstat(existing)
            content = os.read(existing, len(canonical_config) + 1)
            if stat.S_ISREG(info.st_mode) and content == canonical_config:
                return
        except OSError:
            pass
        finally:
            if existing is not None:
                try:
                    os.close(existing)
                except OSError:
                    pass
        temporary_name = f".autoform-config-{os.getpid()}-{secrets.token_hex(8)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: int | None = None
        try:
            if self._scratch_fd is not None:
                descriptor = os.open(
                    temporary_name,
                    flags,
                    0o600,
                    dir_fd=self._scratch_fd,
                )
            else:
                descriptor = os.open(self.scratch / temporary_name, flags, 0o600)
            remaining = memoryview(canonical_config)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short write while installing claim scratch config")
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            if self._scratch_fd is not None:
                os.replace(
                    temporary_name,
                    "config",
                    src_dir_fd=self._scratch_fd,
                    dst_dir_fd=self._scratch_fd,
                )
                os.fsync(self._scratch_fd)
            else:
                os.replace(self.scratch / temporary_name, self.scratch / "config")
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            try:
                if self._scratch_fd is not None:
                    os.unlink(temporary_name, dir_fd=self._scratch_fd)
                else:
                    (self.scratch / temporary_name).unlink()
            except OSError:
                pass
            raise ClaimTransportError(
                "claim scratch Git configuration cannot be installed safely"
            ) from exc

    def _ensure_scratch(self) -> None:
        if self._scratch_identity is not None:
            self._verify_scratch_identity()
        else:
            self.scratch.mkdir(parents=True, exist_ok=True)
            self._scratch_identity = _directory_identity(
                self.scratch,
                label="claim scratch",
                allow_missing=False,
            )
        if self._scratch_ready:
            if (self.scratch / "HEAD").is_symlink():
                raise ClaimTransportError("claim scratch HEAD must not be a symbolic link")
            if not (self.scratch / "HEAD").is_file():
                raise ClaimTransportError("claim scratch is no longer a bare Git repository")
            object_format = self._scratch_object_format()
            if self._object_format != object_format:
                raise ClaimTransportError("claim scratch object format changed")
            self._install_canonical_scratch_config(object_format)
            return
        if (self.scratch / "HEAD").is_symlink():
            raise ClaimTransportError("claim scratch HEAD must not be a symbolic link")
        if (self.scratch / "HEAD").is_file():
            proc = self._git(["rev-parse", "--is-bare-repository"], check=False)
            if proc.returncode != 0 or proc.stdout.strip() != "true":
                raise ClaimTransportError("claim scratch must be a bare Git repository")
            object_format = self._scratch_object_format()
            expected = self._repository_object_format()
            if expected is not None and object_format != expected:
                raise ClaimTransportError(
                    f"claim scratch object format {object_format!r} does not match repository "
                    f"object format {expected!r}"
                )
            self._install_canonical_scratch_config(object_format)
            self._object_format = object_format
            self._scratch_ready = True
            return
        object_format = self._repository_object_format()
        if object_format is None:
            raise ClaimTransportError(
                "cannot determine an empty remote claim repository's object format; "
                "pass expected_object_format"
            )
        self._git(
            [
                "init",
                "--bare",
                "--quiet",
                "--template=",
                f"--object-format={object_format}",
            ]
        )
        if (self.scratch / "HEAD").is_symlink() or not (self.scratch / "HEAD").is_file():
            raise ClaimTransportError("claim scratch initialization could not be verified")
        actual_format = self._scratch_object_format()
        if actual_format != object_format:
            raise ClaimTransportError("claim scratch initialized with the wrong object format")
        self._install_canonical_scratch_config(actual_format)
        self._object_format = actual_format
        self._scratch_ready = True

    @staticmethod
    def _ref(key: str) -> str:
        return CLAIM_REF_PREFIX + _validate_key(key)

    def _receipt_ref(self, key: str) -> str:
        return f"{CLAIM_RECEIPT_REF_PREFIX}{self._session_key}/{_validate_key(key)}"

    def _verify_object_id_format(self, oid: str) -> None:
        if (
            self._object_format is None
            or len(oid) != _OBJECT_FORMAT_LENGTHS[self._object_format]
        ):
            raise ClaimTransportError("claim repository object format changed")

    def _remote_ref_oid(self, ref: str) -> str | None:
        proc = self._remote_git(["ls-remote", self.repo_url, ref])
        entries = _parse_ls_remote_output(proc.stdout)
        if not entries:
            return None
        if len(entries) != 1 or entries[0][1] != ref:
            raise ClaimTransportError(
                f"claim board did not resolve exact requested ref {ref!r}"
            )
        self._verify_object_id_format(entries[0][0])
        return entries[0][0]

    def _remote_oid(self, key: str) -> str | None:
        return self._remote_ref_oid(self._ref(key))

    def _receipt_oid(self, key: str) -> str | None:
        proc = self._git(
            ["rev-parse", "--verify", "--quiet", self._receipt_ref(key)],
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip() or None
        if proc.returncode == 1:
            return None
        detail = (proc.stderr or proc.stdout).strip()[:300]
        raise ClaimTransportError(f"could not read local claim receipt: {detail}")

    def _record_receipt(self, key: str, oid: str, *, expected: str | None = None) -> None:
        args = ["update-ref", self._receipt_ref(key), oid]
        if expected is not None:
            args.append(expected)
        proc = self._git(args, check=False)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise ClaimTransportError(
                "remote claim changed but its exact local ownership receipt could not be recorded"
                + (f": {detail}" if detail else "")
            )

    def _clear_receipt(self, key: str, *, expected: str | None) -> None:
        object_id_width = len(
            self._git(["hash-object", "--stdin"], input_text="").stdout.strip()
        )
        zero_oid = "0" * object_id_width
        args = ["update-ref", self._receipt_ref(key), zero_oid, expected or zero_oid]
        proc = self._git(args, check=False)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise ClaimTransportError(
                "remote claim changed but its local ownership receipt could not be cleared"
                + (f": {detail}" if detail else "")
            )

    def _read_lease(self, key: str, oid: str) -> dict[str, Any]:
        ref = self._ref(key)
        if self._git(["cat-file", "-e", f"{oid}^{{commit}}"], check=False).returncode != 0:
            self._remote_git(
                ["fetch", "--quiet", "--no-write-fetch-head", self.repo_url, f"+{ref}:{ref}"]
            )
        proc = self._git(["cat-file", "commit", oid], check=False)
        if proc.returncode != 0:
            raise MalformedLeaseError(f"claim {key!r} does not point to a readable commit")
        _, separator, message = proc.stdout.partition("\n\n")
        if not separator:
            raise MalformedLeaseError(f"claim {key!r} has no lease message")
        try:
            lease = json.loads(
                message,
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise MalformedLeaseError(f"claim {key!r} has invalid lease JSON") from exc
        if not isinstance(lease, dict) or not self._lease_is_valid(lease, key):
            raise MalformedLeaseError(f"claim {key!r} has an invalid lease schema")
        return lease

    @staticmethod
    def _lease_is_valid(lease: Mapping[str, Any], key: str | None = None) -> bool:
        if lease.get("schema") == LEGACY_BLOCK_SCHEMA:
            return bool(
                isinstance(lease.get("resource"), str)
                and _is_finite_number(lease.get("blocked_at"))
                and isinstance(lease.get("canonical_resource"), str)
                and (key is None or lease.get("resource") == key)
            )
        acquired_at = lease.get("acquired_at")
        renewed_at = lease.get("renewed_at", acquired_at)
        expires_at = lease.get("expires_at")
        valid = (
            lease.get("schema") in {CLAIM_SCHEMA, LEGACY_CLAIM_SCHEMA}
            and isinstance(lease.get("owner"), str)
            and bool(lease.get("owner"))
            and isinstance(lease.get("resource"), str)
            and _is_finite_number(acquired_at)
            and _is_finite_number(expires_at)
            and acquired_at <= expires_at
        )
        if lease.get("schema") == CLAIM_SCHEMA:
            valid = (
                valid
                and _is_finite_number(renewed_at)
                and acquired_at <= renewed_at <= expires_at
                and isinstance(lease.get("lease_id"), str)
                and bool(LEASE_ID_RE.fullmatch(str(lease.get("lease_id"))))
            )
        return bool(valid and (key is None or lease.get("resource") == key))

    def _make_legacy_block_commit(self, key: str, canonical_key: str) -> str:
        key = _validate_key(key)
        canonical_key = _validate_key(canonical_key)
        now = time.time()
        if not math.isfinite(now):
            raise ValueError("claim timestamp must be finite")
        block = {
            "blocked_at": now,
            "canonical_resource": canonical_key,
            "resource": key,
            "schema": LEGACY_BLOCK_SCHEMA,
        }
        tree = self._git(["mktree"], input_text="").stdout.strip()
        message = json.dumps(block, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return self._git(["commit-tree", tree, "-m", message]).stdout.strip()

    def _make_lease_commit(
        self,
        key: str,
        ttl: int | float,
        note: str = "",
        *,
        lease_id: str | None = None,
        acquired_at: int | float | None = None,
        previous_renewed_at: int | float | None = None,
        previous_expires_at: int | float | None = None,
    ) -> str:
        key = _validate_key(key)
        ttl = _validate_ttl(ttl)
        now = time.time()
        if not math.isfinite(now):
            raise ValueError("claim timestamp must be finite")
        if acquired_at is None:
            acquired_at = now
        if not _is_finite_number(acquired_at) or acquired_at > now + CLAIM_CLOCK_SKEW_S:
            raise ValueError(
                "claim acquisition timestamp must be finite and within the allowed clock skew"
            )
        if previous_renewed_at is None:
            previous_renewed_at = acquired_at
        if (
            not _is_finite_number(previous_renewed_at)
            or previous_renewed_at < acquired_at
            or previous_renewed_at > now + CLAIM_CLOCK_SKEW_S
        ):
            raise ValueError(
                "claim renewal timestamp must be monotonic and within the allowed clock skew"
            )
        renewed_at = max(now, acquired_at, previous_renewed_at)
        if previous_expires_at is not None and not _is_finite_number(previous_expires_at):
            raise ValueError("claim expiry timestamp must be finite")
        expiry_floor = renewed_at if previous_expires_at is None else previous_expires_at
        try:
            expires_at = max(renewed_at + ttl, expiry_floor)
        except OverflowError as exc:
            raise ValueError("claim expiry must be finite") from exc
        if not math.isfinite(expires_at):
            raise ValueError("claim expiry must be finite")
        if lease_id is None:
            lease_id = secrets.token_hex(32)
        if not isinstance(lease_id, str) or not LEASE_ID_RE.fullmatch(lease_id):
            raise ValueError("claim lease_id must be 64 lowercase hexadecimal characters")
        lease: dict[str, Any] = {
            "schema": CLAIM_SCHEMA,
            "lease_id": lease_id,
            "owner": self.worker_id,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "acquired_at": acquired_at,
            "renewed_at": renewed_at,
            "expires_at": expires_at,
            "resource": key,
        }
        if note:
            lease["note"] = note
        if not self._lease_is_valid(lease, key):
            raise ValueError("generated claim lease violates the claim schema")
        tree = self._git(["mktree"], input_text="").stdout.strip()
        message = json.dumps(lease, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return self._git(["commit-tree", tree, "-m", message]).stdout.strip()

    def _cas_push(self, key: str, old: str | None, new: str) -> bool:
        ref = self._ref(key)
        source = new if new else ""
        proc = self._remote_git(
            [
                "push",
                "--quiet",
                "--porcelain",
                f"--force-with-lease={ref}:{old or ''}",
                self.repo_url,
                f"{source}:{ref}",
            ],
            check=False,
        )
        if proc.returncode == 0:
            return True
        detail = f"{proc.stdout}\n{proc.stderr}".strip()
        if any(marker in detail.lower() for marker in _CAS_REJECTIONS):
            return False
        # Some Git transports report a compare-and-swap loss only as a generic
        # remote "failed to update ref" error. Re-read the ref: a value that
        # differs from our lease proves another claimant won, while an unchanged
        # value remains a genuine transport failure.
        try:
            current = self._remote_oid(key)
        except ClaimTransportError:
            current = old
        if current == (new or None):
            return True
        if current != old:
            return False
        raise ClaimTransportError(f"claim CAS push failed: {detail[:300]}")

    def read(self, key: str) -> dict[str, Any] | None:
        """Return the current parsed lease, including an expired lease, or ``None``."""
        self._ensure_scratch()
        oid = self._remote_oid(key)
        return self._read_lease(key, oid) if oid else None

    @classmethod
    def expired(cls, lease: Mapping[str, Any], now: float | None = None) -> bool:
        """Return whether a lease is reclaimable after bounded clock-skew grace."""
        comparison_time = time.time() if now is None else now
        if not _is_finite_number(comparison_time):
            raise ValueError("claim expiry comparison clock must be finite")
        if lease.get("schema") in _PERMANENT_BLOCK_SCHEMAS:
            return False
        expires_at = lease.get("expires_at")
        if not _is_finite_number(expires_at):
            return True
        return expires_at <= comparison_time - CLAIM_CLOCK_SKEW_S

    @classmethod
    def _holder_expired(cls, lease: Mapping[str, Any], now: float | None = None) -> bool:
        """Return whether the lease holder's nominal authority has elapsed."""
        comparison_time = time.time() if now is None else now
        if not _is_finite_number(comparison_time):
            raise ValueError("claim expiry comparison clock must be finite")
        if lease.get("schema") in _PERMANENT_BLOCK_SCHEMAS:
            return False
        expires_at = lease.get("expires_at")
        if not _is_finite_number(expires_at):
            return True
        return expires_at <= comparison_time

    @classmethod
    def recovery_required(cls, lease: Mapping[str, Any], now: float | None = None) -> bool:
        """Return whether bounded lease timing was violated and explicit cleanup is required."""
        comparison_time = time.time() if now is None else now
        if not _is_finite_number(comparison_time):
            raise ValueError("claim recovery comparison clock must be finite")
        if lease.get("schema") in _PERMANENT_BLOCK_SCHEMAS:
            return False
        acquired_at = lease.get("acquired_at")
        renewed_at = lease.get("renewed_at", acquired_at)
        expires_at = lease.get("expires_at")
        if (
            not _is_finite_number(acquired_at)
            or not _is_finite_number(renewed_at)
            or not _is_finite_number(expires_at)
        ):
            return False
        return bool(
            renewed_at > comparison_time + CLAIM_CLOCK_SKEW_S
            or (
                lease.get("schema") == CLAIM_SCHEMA
                and expires_at - renewed_at > CLAIM_MAX_TTL_S
            )
        )

    def install_legacy_compatibility(self, key: str, *, canonical_key: str) -> bool:
        """Permanently fence a v1 path key before a v2 canonical claim is used."""
        key = _validate_key(key)
        canonical_key = _validate_key(canonical_key)
        self._ensure_scratch()
        old = self._remote_oid(key)
        if old is not None:
            lease = self._read_lease(key, old)
            if lease.get("schema") == LEGACY_BLOCK_SCHEMA:
                return True
            if (
                lease.get("schema") not in {LEGACY_CLAIM_SCHEMA, CLAIM_SCHEMA}
                or self.recovery_required(lease)
                or not self.expired(lease)
            ):
                return False
        new = self._make_legacy_block_commit(key, canonical_key)
        return self._cas_push(key, old, new)

    def prepare_v2_claim(
        self,
        canonical_key: str,
        compatibility_keys: Iterable[str],
        *,
        canonical_keys: Iterable[str] = (),
    ) -> bool:
        """Retire observable v1 keys, then install permanent compatibility fences."""
        canonical_key = _validate_key(canonical_key)
        keys = tuple(
            key
            for key in dict.fromkeys(_validate_key(key) for key in compatibility_keys)
            if key != canonical_key
        )
        protected = {_validate_key(key) for key in canonical_keys}
        protected.add(canonical_key)
        collisions = sorted(set(keys) & (protected - {canonical_key}))
        if collisions:
            raise ValueError(
                "legacy compatibility key collides with a durable canonical claim key: "
                + ", ".join(collisions)
            )

        for lease in self.list():
            key = str(lease["_key"])
            if not key.startswith("author/"):
                continue
            if lease["_malformed"]:
                raise MalformedLeaseError(
                    f"legacy rollout is blocked by unreadable author claim {key!r}: "
                    f"{lease['_error']}"
                )
            if lease.get("schema") != LEGACY_CLAIM_SCHEMA:
                continue
            if lease["_recovery_required"]:
                raise ClaimTransportError(
                    f"legacy rollout is blocked by unsafe-timestamp claim {key!r}; "
                    "inspect it and run claim cleanup --blueprint PROJECT to recover"
                )
            if not lease["_expired"]:
                return False
            if key in protected:
                continue
            if not self.install_legacy_compatibility(key, canonical_key=canonical_key):
                return False

        for key in keys:
            if not self.install_legacy_compatibility(key, canonical_key=canonical_key):
                return False

        for lease in self.list():
            key = str(lease["_key"])
            if not key.startswith("author/"):
                continue
            if lease["_malformed"]:
                return False
            if lease.get("schema") == LEGACY_CLAIM_SCHEMA and not (
                key in protected and lease["_expired"]
            ):
                return False
        return True

    def _legacy_author_claim_blocks_v2(self, key: str) -> bool:
        if not key.startswith("author/"):
            return False
        for lease in self.list():
            if not str(lease["_key"]).startswith("author/"):
                continue
            if lease["_malformed"]:
                raise MalformedLeaseError(str(lease["_error"]))
            if lease.get("schema") == LEGACY_CLAIM_SCHEMA and (
                lease["_recovery_required"] or not lease["_expired"]
            ):
                return True
        return False

    def acquire(
        self,
        key: str,
        ttl: int | float = CLAIM_TTL_S,
        steal: bool = False,
        note: str = "",
    ) -> bool:
        """CAS-acquire a free or expired lease, or refresh this exact session's lease."""
        key = _validate_key(key)
        _validate_ttl(ttl)
        self._ensure_scratch()
        if self._legacy_author_claim_blocks_v2(key):
            return False
        old = self._remote_oid(key)
        lease_id: str | None = None
        acquired_at: int | float | None = None
        previous_renewed_at: int | float | None = None
        previous_expires_at: int | float | None = None
        if old is not None:
            lease = self._read_lease(key, old)
            if lease.get("schema") in _PERMANENT_BLOCK_SCHEMAS or self.recovery_required(lease):
                return False
            if not self.expired(lease):
                if lease.get("schema") == LEGACY_CLAIM_SCHEMA:
                    return False
                if self._holder_expired(lease):
                    return False
                if not self._receipt_matches(key, old, lease):
                    return False
                else:
                    lease_id = str(lease["lease_id"])
                    acquired_at = lease["acquired_at"]
                    previous_renewed_at = lease.get("renewed_at", acquired_at)
                    previous_expires_at = lease["expires_at"]
        new = self._make_lease_commit(
            key,
            ttl,
            note,
            lease_id=lease_id,
            acquired_at=acquired_at,
            previous_renewed_at=previous_renewed_at,
            previous_expires_at=previous_expires_at,
        )
        if not self._cas_push(key, old, new):
            return False
        if self._legacy_author_claim_blocks_v2(key):
            if not self._cas_push(key, new, old or ""):
                raise ClaimTransportError(
                    "a legacy v1 claim appeared while a v2 claim was acquired, and "
                    "the v2 claim could not be rolled back"
                )
            return False
        self._record_receipt(key, new, expected=old if lease_id is not None else None)
        return True

    def renew(
        self,
        key: str,
        ttl: int | float = CLAIM_TTL_S,
        *,
        lease_id: str | None = None,
    ) -> bool:
        """CAS-renew this session's exact lease, returning ``False`` if it was lost."""
        key = _validate_key(key)
        _validate_ttl(ttl)
        self._ensure_scratch()
        if self._legacy_author_claim_blocks_v2(key):
            return False
        old = self._remote_oid(key)
        if old is None:
            return False
        lease = self._read_lease(key, old)
        if (
            lease.get("schema") != CLAIM_SCHEMA
            or self.recovery_required(lease)
            or self._holder_expired(lease)
            or not self._receipt_matches(key, old, lease)
            or (lease_id is not None and lease.get("lease_id") != lease_id)
        ):
            return False
        new = self._make_lease_commit(
            key,
            ttl,
            str(lease.get("note", "")),
            lease_id=str(lease["lease_id"]),
            acquired_at=lease["acquired_at"],
            previous_renewed_at=lease.get("renewed_at", lease["acquired_at"]),
            previous_expires_at=lease["expires_at"],
        )
        if not self._cas_push(key, old, new):
            return False
        if self._legacy_author_claim_blocks_v2(key):
            if not self._cas_push(key, new, old):
                raise ClaimTransportError(
                    "a legacy v1 claim appeared while a v2 claim was renewed, and "
                    "the prior lease could not be restored"
                )
            return False
        self._record_receipt(key, new, expected=old)
        return True

    def release(self, key: str) -> bool:
        """CAS-delete this session's lease; refuse stale or unverifiable ownership."""
        key = _validate_key(key)
        self._ensure_scratch()
        receipt = self._receipt_oid(key)
        old = self._remote_oid(key)
        if old is None:
            self._clear_receipt(key, expected=receipt)
            return True
        lease = self._read_lease(key, old)
        if (
            lease.get("schema") != CLAIM_SCHEMA
            or self.recovery_required(lease)
            or self._holder_expired(lease)
            or not self._receipt_matches(key, old, lease)
        ):
            return False
        if not self._cas_push(key, old, ""):
            return False
        self._clear_receipt(key, expected=old)
        return True

    def holds(self, key: str) -> bool:
        """Return whether this session has the exact receipt for the live lease."""
        return self.held_claim_oid(key) is not None

    def held_claim_oid(self, key: str) -> str | None:
        """Return the exact live claim commit owned by this session, or ``None``.

        Callers must still use this object ID as a remote compare-and-swap lease.
        Ownership can change immediately after this point-in-time validation.
        """
        fence = self.held_claim_fence(key)
        return fence.oid if fence is not None else None

    def held_lease_id(self, key: str) -> str | None:
        """Return the fenced lease id held by this session, or ``None``."""
        fence = self.held_claim_fence(key)
        return fence.lease_id if fence is not None else None

    def held_claim_fence(self, key: str) -> ClaimFence | None:
        """Return one coherent ref/OID/lease receipt for this session's live claim."""

        held = self._held_claim(key)
        if held is None:
            return None
        oid, lease = held
        return ClaimFence(
            key=key,
            ref=self._ref(key),
            oid=oid,
            lease_id=str(lease["lease_id"]),
        )

    def _held_claim(self, key: str) -> tuple[str, dict[str, Any]] | None:
        key = _validate_key(key)
        self._ensure_scratch()
        if self._legacy_author_claim_blocks_v2(key):
            return None
        oid = self._remote_oid(key)
        if oid is None:
            return None
        lease = self._read_lease(key, oid)
        if (
            lease.get("schema") != CLAIM_SCHEMA
            or self.recovery_required(lease)
            or self._holder_expired(lease)
            or not self._receipt_matches(key, oid, lease)
        ):
            return None
        return oid, lease

    def _receipt_matches(self, key: str, oid: str, lease: Mapping[str, Any]) -> bool:
        """Return whether this session recorded this exact v2 lease commit."""
        receipt_oid = self._receipt_oid(key)
        if receipt_oid != oid or lease.get("schema") != CLAIM_SCHEMA:
            return False
        receipt = self._read_lease(key, receipt_oid)
        return bool(receipt.get("lease_id") == lease.get("lease_id"))

    def list(self) -> list[dict[str, Any]]:
        """Return all claim refs, including malformed and expired entries."""
        self._ensure_scratch()
        proc = self._remote_git(["ls-remote", self.repo_url, CLAIM_REF_PREFIX + "*"])
        leases: list[dict[str, Any]] = []
        seen_refs: set[str] = set()
        for oid, ref in _parse_ls_remote_output(proc.stdout):
            self._verify_object_id_format(oid)
            if not ref.startswith(CLAIM_REF_PREFIX) or ref in seen_refs:
                raise ClaimTransportError(
                    "claim board returned an unexpected or duplicate claim ref"
                )
            seen_refs.add(ref)
            key = ref[len(CLAIM_REF_PREFIX) :]
            try:
                _validate_key(key)
            except ValueError as exc:
                raise ClaimTransportError(
                    f"claim board returned invalid claim ref {ref!r}"
                ) from exc
            try:
                lease = dict(self._read_lease(key, oid))
            except MalformedLeaseError as exc:
                lease = {
                    "resource": key,
                    "schema": "unreadable",
                    "_error": str(exc),
                    "_malformed": True,
                }
            else:
                lease["_malformed"] = False
                lease["_legacy"] = lease.get("schema") == LEGACY_CLAIM_SCHEMA
                lease["_legacy_block"] = lease.get("schema") == LEGACY_BLOCK_SCHEMA
            lease["_key"] = key
            lease["_oid"] = oid
            lease["_expired"] = not lease["_malformed"] and self.expired(lease)
            lease["_recovery_required"] = not lease["_malformed"] and self.recovery_required(
                lease
            )
            leases.append(lease)
        return sorted(leases, key=lambda lease: str(lease["_key"]))

    def cleanup(self, *, canonical_keys: Iterable[str] | None = None) -> int:
        """CAS-recover expired or unsafe leases and return the changed-ref count."""
        protected = (
            None
            if canonical_keys is None
            else {_validate_key(key) for key in canonical_keys}
        )
        leases = self.list()
        if protected is None and any(
            lease.get("schema") == LEGACY_CLAIM_SCHEMA
            and str(lease["_key"]).startswith("author/")
            and (lease["_expired"] or lease["_recovery_required"])
            for lease in leases
        ):
            raise ValueError(
                "a blueprint is required to recover legacy author claims without "
                "blocking durable article IDs"
            )
        recovered = 0
        for lease in leases:
            if lease["_malformed"] or not (
                lease["_expired"] or lease["_recovery_required"]
            ):
                continue
            key = str(lease["_key"])
            old = str(lease["_oid"])
            if lease.get("schema") == LEGACY_CLAIM_SCHEMA and key.startswith("author/"):
                assert protected is not None
                new = (
                    ""
                    if key in protected
                    else self._make_legacy_block_commit(key, "legacy-rollout")
                )
            else:
                new = ""
            if self._cas_push(key, old, new):
                recovered += 1
        return recovered

    def gc(self) -> int:
        """Compatibility alias for :meth:`cleanup`."""
        return self.cleanup()

    def heartbeat(
        self,
        key: str,
        *,
        interval: float = CLAIM_HEARTBEAT_S,
        ttl: int | float = CLAIM_TTL_S,
    ) -> Heartbeat:
        """Create a fail-closed heartbeat for an already acquired lease."""
        return Heartbeat(self, key, interval=interval, ttl=ttl)


class Heartbeat:
    """Renew a lease in a daemon thread and permanently record any uncertainty."""

    def __init__(
        self,
        board: ClaimBoard,
        key: str,
        interval: float = CLAIM_HEARTBEAT_S,
        ttl: int | float = CLAIM_TTL_S,
    ) -> None:
        if not _is_finite_number(interval) or interval <= 0:
            raise ValueError("heartbeat interval must be a finite positive number")
        _validate_key(key)
        _validate_ttl(ttl)
        if interval >= ttl:
            raise ValueError("heartbeat interval must be shorter than the claim TTL")
        self.board = board
        self.key = key
        self.interval = interval
        self.ttl = ttl
        self.lost = threading.Event()
        self.error: Exception | None = None
        self.lease_id: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Heartbeat:
        if self._thread is not None:
            raise RuntimeError("heartbeat cannot be started more than once")
        try:
            self.lease_id = self.board.held_lease_id(self.key)
            renewed = self.lease_id is not None and self.board.renew(
                self.key,
                ttl=self.ttl,
                lease_id=self.lease_id,
            )
        except Exception as exc:
            self.error = exc
            self.lost.set()
            raise ClaimTransportError("claim ownership could not be verified before heartbeat entry") from exc
        if not renewed:
            self.lost.set()
            raise ClaimTransportError("claim ownership was lost before heartbeat entry")
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"claim-heartbeat-{self.key}")
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                renewed = self.board.renew(
                    self.key,
                    ttl=self.ttl,
                    lease_id=self.lease_id,
                )
            except Exception as exc:
                self.error = exc
                self.lost.set()
                return
            if not renewed:
                self.lost.set()
                return


__all__ = [
    "CLAIM_HEARTBEAT_S",
    "CLAIM_KEY_RE",
    "CLAIM_CLOCK_SKEW_S",
    "CLAIM_MAX_TTL_S",
    "CLAIM_RECEIPT_REF_PREFIX",
    "CLAIM_REF_PREFIX",
    "CLAIM_SCHEMA",
    "CLAIM_TTL_S",
    "ClaimBoard",
    "ClaimFence",
    "ClaimTransportError",
    "Heartbeat",
    "LEGACY_CLAIM_SCHEMA",
    "LEGACY_BLOCK_SCHEMA",
    "LEASE_ID_RE",
    "MalformedLeaseError",
    "author_claim_key",
    "claim_repository_is_remote",
    "normalize_claim_repository",
    "pin_claim_repository",
    "pin_claim_scratch",
    "resource_claim_key",
    "workspace_author_claim_key",
]
