"""Client and bootstrap logic for Autoform's node-local Lean runtime.

The two public MCP processes are intentionally short-lived stdio adapters.  A
small detached process owns the expensive Lean REPL pools and LSP sessions and
is reached through a private Unix-domain socket.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
DEFAULT_CONNECT_TIMEOUT = 2.0
DEFAULT_RESPONSE_TIMEOUT = 900.0
DEFAULT_STARTUP_TIMEOUT = 15.0
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PATH_ID = hashlib.sha256(os.fsencode(PACKAGE_ROOT)).hexdigest()[:10]


def _build_id() -> str:
    """Fingerprint code that can change persistent runtime behavior."""
    digest = hashlib.sha256()
    runtime_files = (
        PACKAGE_ROOT / "servers" / "__init__.py",
        Path(__file__).resolve(),
        PACKAGE_ROOT / "servers" / "lean_runtime.py",
        PACKAGE_ROOT / "servers" / "lsp" / "server.py",
        PACKAGE_ROOT / "servers" / "repl" / "core.py",
        PACKAGE_ROOT / "servers" / "repl" / "pool.py",
    )
    for path in runtime_files:
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(os.fsencode(path))
    return digest.hexdigest()[:10]


def _build_generation() -> int:
    """Order in-place builds so an older live wrapper cannot replace a newer one."""
    candidates = (
        Path(__file__).resolve(),
        PACKAGE_ROOT / "servers" / "lean_runtime.py",
        PACKAGE_ROOT / "servers" / "lsp" / "server.py",
        PACKAGE_ROOT / "servers" / "repl" / "core.py",
        PACKAGE_ROOT / "servers" / "repl" / "pool.py",
    )
    mtimes: list[int] = []
    for path in candidates:
        try:
            mtimes.append(path.stat().st_mtime_ns)
        except OSError:
            continue
    return max(mtimes, default=0)


BUILD_ID = _build_id()
BUILD_GENERATION = _build_generation()
INSTALL_ID = f"{INSTALL_PATH_ID}-{BUILD_ID}"
SOCKET_FILENAME = f"lean-v{PROTOCOL_VERSION}-{INSTALL_ID}.sock"


class LeanRuntimeError(RuntimeError):
    """Base error raised by the node-local Lean runtime client."""


class LeanRuntimeUnavailable(LeanRuntimeError):
    """No runtime was listening before a request was dispatched."""


class LeanRuntimeProtocolError(LeanRuntimeError):
    """The runtime spoke an incompatible or malformed protocol."""


class LeanRuntimeRemoteError(LeanRuntimeError):
    """The runtime rejected a well-formed request."""


def _response_timeout_from_environment() -> float:
    raw = os.environ.get(
        "AUTOFORM_RUNTIME_RESPONSE_TIMEOUT",
        str(DEFAULT_RESPONSE_TIMEOUT),
    )
    try:
        value = float(raw)
    except ValueError as error:
        raise LeanRuntimeError(
            f"AUTOFORM_RUNTIME_RESPONSE_TIMEOUT must be a number, got {raw!r}"
        ) from error
    if not math.isfinite(value) or value <= 0:
        raise LeanRuntimeError(
            "AUTOFORM_RUNTIME_RESPONSE_TIMEOUT must be a finite positive number"
        )
    return value


@dataclass(frozen=True)
class RuntimePaths:
    """Filesystem locations used by one per-user runtime instance."""

    directory: Path
    socket: Path
    lock: Path
    lifetime_lock: Path
    log: Path


def _private_runtime_directory(path: Path) -> Path:
    """Create and validate a directory that only the current user can enter."""
    try:
        path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise LeanRuntimeError(f"runtime path is not a directory: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise LeanRuntimeError(f"runtime directory is not owned by this user: {path}")
    if info.st_mode & 0o077:
        raise LeanRuntimeError(
            f"runtime directory must not be accessible by group or other users: {path}"
        )
    return path


def default_runtime_paths() -> RuntimePaths:
    """Return short, node-local paths for the current Unix user."""
    if not hasattr(socket, "AF_UNIX") or not hasattr(os, "getuid"):
        raise LeanRuntimeError("the shared Lean runtime currently requires Unix-domain sockets")

    configured = os.environ.get("AUTOFORM_RUNTIME_DIR")
    if configured:
        directory = Path(configured).expanduser()
        if not directory.is_absolute():
            raise LeanRuntimeError("AUTOFORM_RUNTIME_DIR must be an absolute path")
    else:
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        if xdg and Path(xdg).is_absolute():
            directory = Path(xdg) / "autoform"
        else:
            directory = Path("/tmp") / f"autoform-{os.getuid()}"

    directory = _private_runtime_directory(directory)
    socket_path = directory / SOCKET_FILENAME

    # Most Unix implementations cap sockaddr_un paths at roughly 108 bytes.
    # A uid-specific /tmp fallback remains node-local and is still private.
    if len(os.fsencode(socket_path)) > 100 and not configured:
        directory = _private_runtime_directory(Path("/tmp") / f"autoform-{os.getuid()}")
        socket_path = directory / SOCKET_FILENAME
    if len(os.fsencode(socket_path)) > 100:
        raise LeanRuntimeError(f"Lean runtime socket path is too long: {socket_path}")

    return RuntimePaths(
        directory=directory,
        socket=socket_path,
        # Bootstrap and lifetime locks are installation-wide, rather than
        # build-wide, so two plugin versions cannot race to launch parallel
        # daemons while replacing one another after an in-place upgrade.
        lock=directory / f"lean-v{PROTOCOL_VERSION}-{INSTALL_PATH_ID}.lock",
        lifetime_lock=(
            directory / f"lean-v{PROTOCOL_VERSION}-{INSTALL_PATH_ID}.lifetime.lock"
        ),
        log=directory / f"lean-v{PROTOCOL_VERSION}-{INSTALL_ID}.log",
    )


def runtime_paths_for_socket(socket_path: str | os.PathLike[str]) -> RuntimePaths:
    """Derive lock and log paths for an explicit socket, primarily for tests/CLI."""
    path = Path(socket_path).expanduser()
    if not path.is_absolute():
        raise LeanRuntimeError("Lean runtime socket path must be absolute")
    directory = _private_runtime_directory(path.parent)
    if len(os.fsencode(path)) > 100:
        raise LeanRuntimeError(f"Lean runtime socket path is too long: {path}")
    return RuntimePaths(
        directory=directory,
        socket=path,
        lock=path.with_suffix(".lock"),
        lifetime_lock=path.with_suffix(".lifetime.lock"),
        log=path.with_suffix(".log"),
    )


class LeanRuntimeClient:
    """Make one-request-per-connection calls to the shared Lean runtime."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str] | None = None,
        *,
        autostart: bool = True,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        response_timeout: float | None = None,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    ) -> None:
        self._uses_default_paths = socket_path is None
        self.paths = (
            runtime_paths_for_socket(socket_path)
            if socket_path is not None
            else default_runtime_paths()
        )
        self.autostart = autostart
        self.connect_timeout = connect_timeout
        self.response_timeout = (
            _response_timeout_from_environment()
            if response_timeout is None
            else response_timeout
        )
        self.startup_timeout = startup_timeout

    @property
    def socket_path(self) -> Path:
        return self.paths.socket

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        autostart: bool | None = None,
        response_timeout: float | None = None,
    ) -> Any:
        """Call a runtime method, starting the daemon only before dispatch."""
        should_start = self.autostart if autostart is None else autostart
        try:
            return self._request_once(
                method,
                params or {},
                response_timeout=response_timeout,
            )
        except LeanRuntimeUnavailable:
            if not should_start:
                raise

        self.ensure_running()
        return self._request_once(
            method,
            params or {},
            response_timeout=response_timeout,
        )

    def ping(self, *, autostart: bool = False) -> dict[str, Any]:
        """Return daemon identity without warming a Lean project."""
        result = self.request(
            "daemon.ping",
            autostart=autostart,
            response_timeout=min(self.response_timeout, 5.0),
        )
        if not isinstance(result, dict):
            raise LeanRuntimeProtocolError("daemon.ping returned a non-object result")
        if result.get("install_id") != INSTALL_ID:
            raise LeanRuntimeProtocolError(
                "Lean runtime belongs to a different Autoform installation"
            )
        return result

    def ensure_running(self) -> dict[str, Any]:
        """Race-safely start one detached runtime for this user and node."""
        try:
            return self.ping(autostart=False)
        except LeanRuntimeUnavailable:
            pass

        try:
            import fcntl
        except ImportError as error:  # pragma: no cover - guarded by AF_UNIX above
            raise LeanRuntimeError("runtime bootstrap requires POSIX file locking") from error

        _private_runtime_directory(self.paths.directory)
        lock_fd = os.open(self.paths.lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                return self.ping(autostart=False)
            except LeanRuntimeUnavailable:
                pass

            self._stop_previous_builds()

            # A daemon owns this lock for its complete lifetime. If it has
            # stopped accepting connections but is still draining requests,
            # wait here rather than starting an overlapping replacement.
            lifetime_fd = os.open(
                self.paths.lifetime_lock,
                os.O_CREAT | os.O_RDWR,
                0o600,
            )
            try:
                fcntl.flock(lifetime_fd, fcntl.LOCK_EX)
                self._remove_stale_socket()
                process = self._spawn_daemon()
            finally:
                os.close(lifetime_fd)

            try:
                deadline = time.monotonic() + self.startup_timeout
                delay = 0.025
                last_error: BaseException | None = None
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    try:
                        return self.ping(autostart=False)
                    except LeanRuntimeUnavailable as error:
                        last_error = error
                    time.sleep(delay)
                    delay = min(delay * 1.7, 0.25)

                exit_detail = (
                    f" (exit code {process.returncode})"
                    if process.poll() is not None
                    else ""
                )
                detail = f": {last_error}" if last_error else ""
                raise LeanRuntimeUnavailable(
                    f"Lean runtime did not become ready{exit_detail}; "
                    f"log: {self.paths.log}{detail}"
                )
            except BaseException:
                self._terminate_failed_start(process)
                raise
        finally:
            os.close(lock_fd)

    def stop(self) -> dict[str, Any]:
        """Ask a running daemon to finish active calls and shut down."""
        try:
            result = self.request(
                "daemon.shutdown",
                autostart=False,
                response_timeout=10.0,
            )
        except LeanRuntimeUnavailable:
            stopped = self._stop_previous_builds()
            if stopped:
                return {"stopping": False, "stopped_previous": stopped}
            raise
        if not isinstance(result, dict):
            raise LeanRuntimeProtocolError("daemon.shutdown returned a non-object result")
        deadline = time.monotonic() + self.response_timeout
        while self.paths.socket.exists() and time.monotonic() < deadline:
            time.sleep(0.025)
        if self.paths.socket.exists():
            raise LeanRuntimeError(
                f"Lean runtime is still draining requests at {self.paths.socket}"
            )
        return result

    def _stop_previous_builds(self) -> list[int]:
        """Gracefully replace older code generations at the same install path."""
        if not self._uses_default_paths:
            return []
        pattern = f"lean-v{PROTOCOL_VERSION}-{INSTALL_PATH_ID}-*.sock"
        stopped: list[int] = []
        for socket_path in self.paths.directory.glob(pattern):
            if socket_path == self.paths.socket:
                continue
            previous = LeanRuntimeClient(
                socket_path=socket_path,
                autostart=False,
                connect_timeout=self.connect_timeout,
                response_timeout=self.response_timeout,
                startup_timeout=self.startup_timeout,
            )
            try:
                status = previous.request("daemon.ping", autostart=False)
            except LeanRuntimeUnavailable:
                continue
            generation = (
                status.get("build_generation") if isinstance(status, dict) else None
            )
            if isinstance(generation, int) and generation > BUILD_GENERATION:
                raise LeanRuntimeProtocolError(
                    "a newer Autoform runtime build is already active; "
                    "restart this plugin session before using Lean tools"
                )
            result = previous.stop()
            pid = result.get("pid") if isinstance(result, dict) else None
            if isinstance(pid, int):
                stopped.append(pid)
        return stopped

    def _spawn_daemon(self) -> subprocess.Popen[bytes]:
        command = [
            sys.executable,
            "-m",
            "servers.lean_runtime",
            "--socket",
            str(self.paths.socket),
            "--log",
            str(self.paths.log),
            "serve",
        ]
        with self.paths.log.open("ab", buffering=0) as log:
            return subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
                cwd=PACKAGE_ROOT,
            )

    def _remove_stale_socket(self) -> None:
        try:
            info = self.paths.socket.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode):
            raise LeanRuntimeError(
                f"refusing to replace non-socket runtime path: {self.paths.socket}"
            )
        if info.st_uid != os.getuid():
            raise LeanRuntimeError(
                f"refusing to replace socket owned by another user: {self.paths.socket}"
            )
        self.paths.socket.unlink()

    @staticmethod
    def _terminate_failed_start(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _request_once(
        self,
        method: str,
        params: dict[str, Any],
        *,
        response_timeout: float | None = None,
    ) -> Any:
        request_id = uuid.uuid4().hex
        payload = json.dumps(
            {
                "v": PROTOCOL_VERSION,
                "id": request_id,
                "method": method,
                "params": params,
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        if len(payload) > MAX_MESSAGE_BYTES:
            raise LeanRuntimeProtocolError("Lean runtime request exceeds the message limit")

        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dispatched = False
        try:
            connection.settimeout(self.connect_timeout)
            try:
                connection.connect(str(self.paths.socket))
            except (FileNotFoundError, ConnectionRefusedError) as error:
                raise LeanRuntimeUnavailable(
                    f"Lean runtime is not listening at {self.paths.socket}"
                ) from error
            except OSError as error:
                if error.errno in {2, 61, 111}:
                    raise LeanRuntimeUnavailable(
                        f"Lean runtime is not listening at {self.paths.socket}"
                    ) from error
                raise LeanRuntimeError(f"cannot connect to Lean runtime: {error}") from error

            connection.settimeout(response_timeout or self.response_timeout)
            # From this point onward, any failure is ambiguous: the daemon may
            # have received the request. Never auto-replay Lean execution.
            dispatched = True
            connection.sendall(payload)
            raw = self._read_line(connection)
        except socket.timeout as error:
            phase = "response" if dispatched else "connection"
            raise LeanRuntimeError(f"timed out waiting for Lean runtime {phase}") from error
        except LeanRuntimeError:
            raise
        except OSError as error:
            if not dispatched:
                raise LeanRuntimeUnavailable(
                    f"Lean runtime is not listening at {self.paths.socket}"
                ) from error
            raise LeanRuntimeError(
                "connection to Lean runtime closed after request dispatch; the request was not retried"
            ) from error
        finally:
            connection.close()

        try:
            response = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LeanRuntimeProtocolError("Lean runtime returned invalid JSON") from error
        if not isinstance(response, dict):
            raise LeanRuntimeProtocolError("Lean runtime response is not an object")
        if response.get("v") != PROTOCOL_VERSION:
            raise LeanRuntimeProtocolError(
                f"Lean runtime protocol mismatch: expected {PROTOCOL_VERSION}, got {response.get('v')!r}"
            )
        if response.get("id") != request_id:
            raise LeanRuntimeProtocolError("Lean runtime response id does not match the request")
        if response.get("ok") is True:
            return response.get("result")
        error = response.get("error")
        if not isinstance(error, dict):
            raise LeanRuntimeProtocolError("Lean runtime returned a malformed error")
        error_type = error.get("type", "RuntimeError")
        message = error.get("message", "unspecified runtime error")
        raise LeanRuntimeRemoteError(f"{error_type}: {message}")

    @staticmethod
    def _read_line(connection: socket.socket) -> str:
        data = bytearray()
        while len(data) <= MAX_MESSAGE_BYTES:
            chunk = connection.recv(min(65536, MAX_MESSAGE_BYTES + 1 - len(data)))
            if not chunk:
                raise LeanRuntimeProtocolError("Lean runtime closed without a response")
            data.extend(chunk)
            newline = data.find(b"\n")
            if newline >= 0:
                if data[newline + 1 :]:
                    raise LeanRuntimeProtocolError("Lean runtime returned trailing response data")
                return bytes(data[:newline]).decode("utf-8")
        raise LeanRuntimeProtocolError("Lean runtime response exceeds the message limit")
