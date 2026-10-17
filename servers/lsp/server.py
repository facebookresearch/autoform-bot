"""Lean hover over LSP, and the MCP adapter for the LSP tools.

Wraps Lean 4 language server processes and provides hover information
independently from the REPL pool. Diagnostics come from
``servers/batch_diagnostics.py``; this module only formats them.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from typing import Any, TypeVar

from fastmcp.server import FastMCP

from servers.lean_client import LeanRuntimeClient
from servers.repl.core import (
    REPL_ABORT_KILL_SECONDS,
    REPL_ABORT_TERM_SECONDS,
    _process_group_has_live_members,
    _reap_process,
    _terminate_process_group,
    _wait_for_live_process_group_exit,
)

logger = getLogger(__name__)
# Bound at import: tests swap subprocess.Popen for factories, and non-Popen
# fakes skip the psutil liveness probe.
_POPEN_TYPE = subprocess.Popen
T = TypeVar("T")

DEFAULT_LSP_TIMEOUT = 60
MAX_LSP_HEADER_BYTES = 16 * 1024
MAX_LSP_MESSAGE_BYTES = 16 * 1024 * 1024
# How long Lean's watchdog gets to stop its file workers once its pipes close.
LSP_EOF_GRACE_SECONDS = 2.0
# How long close() waits for Lean to leave after shutdown and exit.
LSP_SHUTDOWN_GRACE_SECONDS = 5.0
# Names the random token start() puts in the Lean server's environment. Every
# process the server starts inherits it, in any process group, orphaned or not,
# unless a process between them starts it without the variable.
_SESSION_TOKEN_ENV = "_AUTOFORM_LSP_SESSION_TOKEN"


class LspProtocolError(RuntimeError):
    """The Lean language server returned or emitted invalid JSON-RPC state."""


class LspBusyError(TimeoutError):
    """A queued operation could not enter the shared LSP session in time."""


class LeanLspStartupError(RuntimeError):
    """Startup failed while the session still owns process handles."""

    def __init__(self, session: LeanLspSession, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.session = session
        self.cause = cause


@dataclass
class LspConfig:
    """Configuration for the Lean LSP server."""

    cwd: str = "."
    lake_command: list[str] = field(default_factory=lambda: ["lake", "serve"])
    timeout: float = DEFAULT_LSP_TIMEOUT


def _processes_with_token(token: str) -> list[Any]:
    """Return the processes whose environment carries a session's token.

    A process whose environment cannot be read is left out, so it is never
    signalled: nothing shows that it is the session's. Lean and Lake run as
    the user, who can read their environments; setuid programs the user runs,
    such as ``login``, are unreadable at all times, so counting them would
    fail every close. A process table that cannot be read fails the scan.
    """
    if not token:
        # A missing token would match every process without the variable.
        raise ValueError("a Lean LSP session token must be a nonempty string")
    import psutil

    try:
        pids = psutil.pids()
    except (OSError, psutil.Error) as error:
        raise RuntimeError("cannot read the process table to find the Lean LSP session's processes") from error
    found = []
    for pid in pids:
        if pid == os.getpid():
            continue
        try:
            # The handle records the creation time before the environment is
            # read, so it can only name the process that was read or an
            # earlier one, which psutil then refuses to signal.
            candidate = psutil.Process(pid)
            environment = candidate.environ()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            # Exited or unreadable; a zombie reads as exited or empty.
            continue
        if environment.get(_SESSION_TOKEN_ENV) == token:
            found.append(candidate)
    return found


def _wait_for_exit(processes: list[Any], deadline: float) -> None:
    """Wait until every process has exited or become a zombie, or the deadline passes."""
    import psutil

    def running(process: Any) -> bool:
        try:
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
        except psutil.Error:
            return True

    while any(running(process) for process in processes) and time.monotonic() < deadline:
        time.sleep(0.01)


class LeanLspSession:
    """Manages a Lean 4 language server subprocess via JSON-RPC."""

    def __init__(self, config: LspConfig) -> None:
        self.config = config
        self.process: subprocess.Popen | None = None
        self._process_group_id: int | None = None
        # Set by start(); every process the server starts carries it.
        self._token: str | None = None
        self._group_retired = False
        self._retire_pending = False
        self._request_id = 0
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        # A session has one stdout stream. Serialize the complete document
        # lifecycle so concurrent MCP calls cannot race two readers against
        # that stream or consume one another's responses.
        self._operation_lock = threading.Lock()

    def start(self) -> None:
        """Start the language server process."""
        if os.name != "posix":
            raise RuntimeError("Lean LSP transport requires a POSIX platform")
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        self._token = secrets.token_hex(16)
        env[_SESSION_TOKEN_ENV] = self._token
        process: subprocess.Popen | None = None
        try:
            process = subprocess.Popen(
                self.config.lake_command,
                cwd=self.config.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
            self.process = process
            self._process_group_id = process.pid
            self._group_retired = False
            self._retire_pending = False
            # An initialize response must be an InitializeResult object. A
            # timeout, JSON-RPC error, or malformed result means the backing
            # Lean server is unusable, so do not expose an apparently healthy
            # MCP server on top of it.
            result = self._send_request("initialize", {
                "processId": os.getpid(),
                "capabilities": {},
                "rootUri": Path(self.config.cwd).resolve().as_uri(),
            })
            if not isinstance(result, dict):
                raise LspProtocolError(
                    "LSP initialize returned a non-object result: "
                    f"{result!r}"
                )

            self._send_notification("initialized", {})
        except BaseException as error:
            if self.process is None and process is not None:
                self.process = process
                self._process_group_id = process.pid
                self._group_retired = False
            try:
                self._retire_once()
            except BaseException as cleanup_error:
                logger.exception("failed to retire Lean LSP process after startup failure")
                raise LeanLspStartupError(self, error) from cleanup_error
            raise

    def _retire_once(self) -> None:
        """Advance one owned process through verified retirement once."""
        self._retire_pending = True
        process = self.process
        if process is None:
            self._retire_pending = False
            return
        if not self._group_retired:
            process_group_id = self._process_group_id
            if process_group_id is None:
                process_group_id = process.pid
                self._process_group_id = process_group_id
            if getattr(process, "returncode", None) is not None:
                if _process_group_has_live_members(process_group_id):
                    raise RuntimeError(
                        "refusing to signal an LSP process group after its leader was reaped"
                    )
            else:
                # Lean's watchdog starts each file worker in its own session
                # and stops worker groups itself when its stdin closes. A TERM
                # first would kill the watchdog and orphan workers the group
                # signal cannot reach. Close stdout too: the watchdog writes
                # during that shutdown and handles a closed pipe, but would
                # block on a full one. The leader stays unreaped while the
                # watchdog exits, so the PGID cannot be reused meanwhile.
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            logger.warning("failed to close a Lean LSP pipe", exc_info=True)
                _wait_for_live_process_group_exit(
                    process_group_id, time.monotonic() + LSP_EOF_GRACE_SECONDS
                )
                _terminate_process_group(process_group_id)
            # Publish this before any operation that can reap the leader. A
            # retry must never signal a reused numeric PGID.
            self._group_retired = True
            self._process_group_id = None
        self._stop_processes_with_token()
        _reap_process(process)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()
        self.process = None
        self._group_retired = False
        self._retire_pending = False

    def _stop_processes_with_token(self) -> None:
        """Terminate every live process that carries this session's token.

        Lean's watchdog starts each file worker in its own session. As it
        shuts down it skips a worker that has already exited, whose builds
        keep running, and a watchdog that dies first orphans every worker.
        None of them is in the server's group, but each carries the token.
        Raise while any is left, so the caller retries before reaping the
        leader.
        """
        token = self._token
        if token is None:
            return
        import psutil

        for signal_number, seconds in (
            (signal.SIGTERM, REPL_ABORT_TERM_SECONDS),
            (signal.SIGKILL, REPL_ABORT_KILL_SECONDS),
        ):
            processes = _processes_with_token(token)
            if not processes:
                return
            for process in processes:
                # psutil checks the PID's creation time before signalling, so a
                # process that took over the PID after the scan is spared.
                try:
                    process.send_signal(signal_number)
                except (OSError, psutil.Error):
                    pass
            _wait_for_exit(processes, time.monotonic() + seconds)
        if _processes_with_token(token):
            raise RuntimeError("processes started by the Lean LSP server are still running")

    def _abort_process(self) -> None:
        """Retry verified retirement without ever surrendering ownership."""
        delay = 0.01
        while self.process is not None:
            try:
                self._retire_once()
            except BaseException:
                logger.exception("failed to retire the Lean LSP process group; retrying")
                time.sleep(delay)
                delay = min(delay * 2, 1.0)

    def close(self) -> None:
        """Shut down the language server."""
        with self._lifecycle_lock:
            if self.is_alive():
                try:
                    self._send_request("shutdown", {})
                    # Lean leaves after shutdown only on an exit without params.
                    self._send_notification("exit", None)
                    if self._process_group_id is not None:
                        _wait_for_live_process_group_exit(
                            self._process_group_id,
                            time.monotonic() + LSP_SHUTDOWN_GRACE_SECONDS,
                        )
                except Exception:
                    logger.warning("graceful Lean LSP shutdown failed", exc_info=True)
            self._abort_process()

    def abort(self) -> None:
        """Discard a protocol stream that can no longer be shared safely."""
        with self._lifecycle_lock:
            self._abort_process()

    def retire(self) -> None:
        """Prevent new work while the owner transfers cleanup to a reaper."""
        self._retire_pending = True

    def is_alive(self) -> bool:
        """Return whether the cached language-server child can accept work."""
        process = self.process
        if (
            process is None
            or self._retire_pending
            or getattr(process, "returncode", None) is not None
        ):
            return False
        if not isinstance(process, _POPEN_TYPE):
            return True
        try:
            import psutil

            status = psutil.Process(process.pid).status()
        except psutil.NoSuchProcess:
            return False
        except psutil.Error:
            return True
        return status not in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}

    def _admitted(self, operation: Callable[[float], T]) -> T:
        """Run one serialized operation and fence a failed protocol stream."""
        deadline = time.monotonic() + self.config.timeout
        if not self._operation_lock.acquire(timeout=self.config.timeout):
            raise LspBusyError(
                f"timed out after {self.config.timeout:g}s waiting for the Lean LSP session"
            )
        try:
            if self._retire_pending:
                raise LspProtocolError("Lean LSP session is retiring after a failed operation")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LspBusyError(
                    f"timed out after {self.config.timeout:g}s waiting for the Lean LSP session"
                )
            try:
                return operation(remaining)
            except BaseException:
                self._retire_pending = True
                raise
        finally:
            self._operation_lock.release()

    def hover(self, file_path: str, line: int, character: int) -> str | None:
        """Get hover information at a position."""
        # Input failures happen before admission and cannot desynchronize the
        # shared protocol stream, so they must not retire a healthy session.
        path = Path(file_path).resolve()
        content = path.read_text()
        return self._admitted(
            lambda remaining: self._hover(
                path,
                content,
                line,
                character,
                timeout=remaining,
            )
        )

    def _hover(
        self,
        path: Path,
        content: str,
        line: int,
        character: int,
        *,
        timeout: float = 30,
    ) -> str | None:
        uri = path.as_uri()
        deadline = time.monotonic() + timeout
        self._open_document(uri, content, timeout=self._remaining(deadline, timeout))
        try:
            result = self._send_request(
                "textDocument/hover",
                {
                    "textDocument": {"uri": uri},
                    "position": {"line": line, "character": character},
                },
                timeout=self._remaining(deadline, timeout),
            )
        finally:
            try:
                self._send_notification(
                    "textDocument/didClose",
                    {"textDocument": {"uri": uri}},
                    timeout=self._remaining(deadline, timeout),
                )
            except Exception:
                self._retire_pending = True
                logger.warning("failed to close LSP document %s", uri, exc_info=True)
        if result and "contents" in result:
            contents = result["contents"]
            if isinstance(contents, dict):
                return contents.get("value", "")
            return str(contents)
        return None

    def _open_document(self, uri: str, content: str, *, timeout: float) -> None:
        """Send didOpen. Nothing reads a document version back, so every open is version 1."""
        self._send_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "lean4",
                    "version": 1,
                    "text": content,
                }
            },
            timeout=timeout,
        )

    def _send_request(self, method: str, params: dict, *, timeout: float = 30) -> Any:
        """Send a JSON-RPC request and wait for response."""
        deadline = time.monotonic() + timeout
        with self._lock:
            self._request_id += 1
            msg = {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            }
            self._write_message(msg, timeout=self._remaining(deadline, timeout))
            return self._read_response(
                self._request_id,
                timeout=self._remaining(deadline, timeout),
            )

    def _send_notification(
        self,
        method: str,
        params: dict | None,
        *,
        timeout: float = 30,
    ) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write_message(msg, timeout=timeout)

    def _write_message(self, msg: dict, *, timeout: float = 30) -> None:
        """Write a bounded JSON-RPC message without blocking past timeout."""
        if not self.process or not self.process.stdin:
            raise RuntimeError("LSP process not running")
        body = json.dumps(msg).encode("utf-8")
        if len(body) > MAX_LSP_MESSAGE_BYTES:
            raise LspProtocolError(
                f"LSP request exceeds {MAX_LSP_MESSAGE_BYTES} bytes"
            )
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        data = header + body
        data_view = memoryview(data)
        stdin_fd = self.process.stdin.fileno()
        os.set_blocking(stdin_fd, False)
        deadline = time.monotonic() + timeout
        offset = 0

        import select as _select

        while offset < len(data):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"timed out after {timeout:g}s writing an LSP message"
                )
            _, writable, _ = _select.select([], [stdin_fd], [], remaining)
            if not writable:
                raise TimeoutError(
                    f"timed out after {timeout:g}s writing an LSP message"
                )
            try:
                written = os.write(stdin_fd, data_view[offset:])
            except BlockingIOError:
                continue
            if written <= 0:
                raise LspProtocolError("LSP process closed stdin mid-message")
            offset += written

    def _read_response(self, request_id: int, timeout: float = 30) -> Any:
        """Read JSON-RPC messages until we get the response for request_id."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            msg = self._read_message(timeout=remaining)
            # Lean's workers send their own requests with numeric ids, so a
            # frame that names a method is never the response to ours.
            if msg and "method" not in msg and msg.get("id") == request_id:
                return self._response_result(msg, request_id)
        raise TimeoutError(
            f"timed out after {timeout:g}s waiting for LSP response {request_id}"
        )

    @staticmethod
    def _response_result(msg: dict, request_id: int) -> Any:
        """Return a JSON-RPC response's result, or raise its error."""
        if "error" in msg:
            error = msg["error"]
            if isinstance(error, dict):
                code = error.get("code", "unknown")
                message = error.get("message", "unspecified protocol error")
                data = error.get("data")
                detail = f" ({data!r})" if data is not None else ""
                raise LspProtocolError(
                    f"LSP request {request_id} failed [{code}]: "
                    f"{message}{detail}"
                )
            raise LspProtocolError(
                f"LSP request {request_id} returned malformed error: {error!r}"
            )
        if "result" not in msg:
            raise LspProtocolError(
                f"LSP response {request_id} has neither result nor error"
            )
        return msg["result"]

    def _read_message(self, timeout: float = 5) -> dict | None:
        """Read one bounded JSON-RPC frame within one absolute deadline."""
        if not self.process or not self.process.stdout:
            raise RuntimeError("LSP process not running")

        import select as _select

        stdout_fd = self.process.stdout.fileno()
        deadline = time.monotonic() + timeout

        # Read Content-Length header, checking the deadline before every byte.
        header = bytearray()
        while b"\r\n\r\n" not in header:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if not header:
                    return None
                raise TimeoutError(
                    f"timed out after {timeout:g}s reading an LSP header"
                )
            ready, _, _ = _select.select([stdout_fd], [], [], remaining)
            if not ready:
                if not header:
                    return None
                raise TimeoutError(
                    f"timed out after {timeout:g}s reading an LSP header"
                )
            chunk = os.read(stdout_fd, 1)
            if not chunk:
                raise LspProtocolError("LSP process closed stdout mid-header")
            header.extend(chunk)
            if len(header) > MAX_LSP_HEADER_BYTES:
                raise LspProtocolError(
                    f"LSP header exceeds {MAX_LSP_HEADER_BYTES} bytes"
                )

        try:
            header_lines = bytes(header[:-4]).decode("ascii").split("\r\n")
        except UnicodeDecodeError as error:
            raise LspProtocolError("LSP emitted a non-ASCII header") from error
        content_lengths = [
            value.strip()
            for line in header_lines
            if ":" in line
            for name, value in [line.split(":", 1)]
            if name.strip().lower() == "content-length"
        ]
        if len(content_lengths) != 1:
            raise LspProtocolError(
                f"LSP message has {len(content_lengths)} Content-Length headers"
            )
        try:
            length = int(content_lengths[0])
        except ValueError as error:
            raise LspProtocolError(
                f"invalid LSP Content-Length: {content_lengths[0]!r}"
            ) from error
        if length < 0:
            raise LspProtocolError(f"invalid negative LSP Content-Length: {length}")
        if length > MAX_LSP_MESSAGE_BYTES:
            raise LspProtocolError(
                f"LSP message exceeds {MAX_LSP_MESSAGE_BYTES} bytes"
            )

        # Read the body in available chunks, never blocking past the deadline.
        body = bytearray()
        while len(body) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"timed out after {timeout:g}s reading an LSP body"
                )
            ready, _, _ = _select.select([stdout_fd], [], [], remaining)
            if not ready:
                raise TimeoutError(
                    f"timed out after {timeout:g}s reading an LSP body"
                )
            chunk = os.read(stdout_fd, length - len(body))
            if not chunk:
                raise LspProtocolError("LSP process closed stdout mid-message")
            body.extend(chunk)

        try:
            message = json.loads(bytes(body).decode("utf-8"))
        except (ValueError, RecursionError) as error:
            raise LspProtocolError("LSP emitted an invalid JSON body") from error
        if not isinstance(message, dict):
            raise LspProtocolError("LSP JSON-RPC message is not an object")
        return message

    @staticmethod
    def _remaining(deadline: float, timeout: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"LSP operation timed out after {timeout:g}s")
        return remaining


def format_lsp_diagnostics(diagnostics: list[dict]) -> str:
    """Format Lean's LSP diagnostics for the MCP tool response."""
    if not diagnostics:
        return "No diagnostics — file compiles cleanly."

    lines: list[str] = []
    for diagnostic in diagnostics:
        severity = {1: "error", 2: "warning", 3: "info", 4: "hint"}.get(
            diagnostic.get("severity", 0), "unknown"
        )
        position = diagnostic.get("range", {}).get("start", {})
        line = position.get("line", 0) + 1
        column = position.get("character", 0)
        message = diagnostic.get("message", "")
        lines.append(f"{line}:{column}: {severity}: {message}")

    errors = sum(item.get("severity") == 1 for item in diagnostics)
    warnings = sum(item.get("severity") == 2 for item in diagnostics)
    return f"Diagnostics: {errors} error(s), {warnings} warning(s)\n" + "\n".join(lines)


def create_lsp_server(runtime: LeanRuntimeClient) -> FastMCP:
    """Create the public LSP MCP adapter for the shared Lean runtime."""
    server = FastMCP(name="autoform-lsp")

    @server.tool
    def lean_diagnostic_messages(project_dir: str, file_path: str) -> str:
        """Return Lean diagnostics for an in-project file.

        Runs ``lake --no-build lean`` on the file once and waits for Lean to exit.
        The runtime's LEAN_LSP_TIMEOUT (60 seconds by default) bounds the call,
        including any wait for a free Lean slot; a file that needs longer fails
        instead of returning partial diagnostics. Unbuilt imports are reported,
        not built: build the listed modules and call again.

        Args:
            project_dir: Absolute path to the Lake project root.
            file_path: Absolute path, or a path relative to project_dir, to a Lean file.
        """
        return runtime.request(
            "lsp.diagnostics",
            {"project_dir": project_dir, "file_path": file_path},
        )

    @server.tool
    def lean_hover(project_dir: str, file_path: str, line: int, character: int) -> str:
        """Return Lean hover information at a zero-indexed position.

        Args:
            project_dir: Absolute path to the Lake project root.
            file_path: Absolute path, or a path relative to project_dir, to a Lean file.
            line: Zero-indexed line number.
            character: Zero-indexed character position.
        """
        return runtime.request(
            "lsp.hover",
            {
                "project_dir": project_dir,
                "file_path": file_path,
                "line": line,
                "character": character,
            },
        )

    return server


def main() -> None:
    create_lsp_server(LeanRuntimeClient()).run(transport="stdio")


if __name__ == "__main__":
    main()
