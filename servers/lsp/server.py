"""Lean execution backend for diagnostics and type information over LSP.

Wraps Lean 4 language server processes and provides file diagnostics and hover
information independently from the REPL pool.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp.server import FastMCP

from servers import resolve_lean_project_dir
from servers.lean_client import LeanRuntimeClient
from servers.repl.core import _kill_subprocesses

logger = getLogger(__name__)

DEFAULT_LSP_TIMEOUT = 60
DEFAULT_LSP_CLEANUP_SECONDS = 2.0
LSP_CLEANUP_RETRY_SECONDS = 0.05
MAX_LSP_CLEANUP_RETRY_SECONDS = 1.0
MAX_LSP_HEADER_BYTES = 16 * 1024
MAX_LSP_MESSAGE_BYTES = 16 * 1024 * 1024


class LspProtocolError(RuntimeError):
    """The Lean language server returned or emitted invalid JSON-RPC state."""


class LspBusyError(TimeoutError):
    """A queued operation could not enter the shared LSP session in time."""


@dataclass
class LspConfig:
    """Configuration for the Lean LSP server."""

    cwd: str = "."
    lake_command: list[str] = field(default_factory=lambda: ["lake", "serve"])
    timeout: float = DEFAULT_LSP_TIMEOUT


class LeanLspSession:
    """Manages a Lean 4 language server subprocess via JSON-RPC."""

    def __init__(self, config: LspConfig) -> None:
        self.config = config
        self.process: subprocess.Popen | None = None
        self._process_group_id: int | None = None
        self._retire_pending = False
        self._request_id = 0
        self._lock = threading.Lock()
        # A session has one stdout stream. Serialize the complete document
        # lifecycle so concurrent MCP calls cannot race two readers against
        # that stream or consume one another's diagnostics/responses.
        self._operation_lock = threading.Lock()

    def start(self) -> None:
        """Start the language server process."""
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)

        try:
            self.process = subprocess.Popen(
                self.config.lake_command,
                cwd=self.config.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
            self._process_group_id = self.process.pid
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
            self._abort_until_clean(original_error=error)
            raise

    @staticmethod
    def _close_process_streams(process: subprocess.Popen) -> BaseException | None:
        """Close every pipe, retaining any cancellation raised after closure."""
        cancellation: BaseException | None = None
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, stream_name, None)
            if stream is None or stream.closed:
                continue
            try:
                stream.close()
            except BaseException as error:
                if not stream.closed:
                    # The descriptor may still be owned. Retain the process
                    # handle so a later close can retry without an fd leak.
                    raise
                if not isinstance(error, Exception) and cancellation is None:
                    cancellation = error
                else:
                    logger.warning(
                        "Lean LSP pipe reported an error after closing",
                        exc_info=True,
                    )
        return cancellation

    def _abort_process(self) -> None:
        """Force-close and verify the complete backing process group."""
        process = self.process
        process_group_id = self._process_group_id
        if process is None:
            if process_group_id is not None:
                raise RuntimeError("Lean LSP process-group ownership is inconsistent")
            self._retire_pending = False
            return

        self._retire_pending = True
        if process_group_id is None:
            process_group_id = process.pid
            self._process_group_id = process_group_id

        cancellation: BaseException | None = None
        try:
            _kill_subprocesses(
                process,
                process_group_id,
                time.monotonic() + DEFAULT_LSP_CLEANUP_SECONDS,
            )
            cancellation = self._close_process_streams(process)
        except BaseException:
            # Keep both handles so the cache can quarantine this session and a
            # later close can retry without losing ownership.
            raise
        else:
            self.process = None
            self._process_group_id = None
            self._retire_pending = False
        if cancellation is not None:
            raise cancellation.with_traceback(cancellation.__traceback__)

    def _abort_until_clean(
        self,
        *,
        original_error: BaseException | None = None,
    ) -> None:
        """Retain failed-start ownership until cleanup can be verified."""
        delay = LSP_CLEANUP_RETRY_SECONDS
        cancellation: BaseException | None = None
        while True:
            try:
                self._abort_process()
                break
            except BaseException as error:
                if not isinstance(error, Exception) and cancellation is None:
                    cancellation = error
                logger.exception(
                    "Lean LSP startup cleanup remains incomplete; retaining ownership"
                )
                time.sleep(delay)
                delay = min(delay * 2, MAX_LSP_CLEANUP_RETRY_SECONDS)
        if cancellation is not None:
            if original_error is not None:
                add_note = getattr(cancellation, "add_note", None)
                if add_note is not None:
                    add_note(f"Lean LSP startup also failed: {original_error}")
            raise cancellation.with_traceback(cancellation.__traceback__)

    def close(self) -> None:
        """Shut down gracefully, then verify the whole process group is gone."""
        process = self.process
        cancellation: BaseException | None = None
        if process is not None and not self._retire_pending and process.poll() is None:
            try:
                self._send_request("shutdown", {})
                self._send_notification("exit", {})
                process.wait(timeout=5)
            except Exception:
                # A failed graceful exchange is expected to fall through to the
                # verified process-group cleanup below.
                pass
            except BaseException as error:
                cancellation = error
        try:
            self._abort_process()
        except BaseException as cleanup_error:
            if cancellation is not None:
                add_note = getattr(cancellation, "add_note", None)
                if add_note is not None:
                    add_note(f"Lean LSP process cleanup also failed: {cleanup_error}")
                raise cancellation.with_traceback(cancellation.__traceback__)
            raise
        if cancellation is not None:
            raise cancellation.with_traceback(cancellation.__traceback__)

    def abort(self) -> None:
        """Discard a protocol stream that can no longer be shared safely."""
        self._abort_process()

    def is_alive(self) -> bool:
        """Return whether the cached language-server child can accept work."""
        return (
            self.process is not None
            and not self._retire_pending
            and self.process.poll() is None
        )

    def is_clean(self) -> bool:
        """Return whether no live or unreaped LSP process group is owned."""
        return self.process is None and self._process_group_id is None

    def get_diagnostics(self, file_path: str) -> list[dict]:
        """Open a file and collect diagnostics from the language server."""
        deadline = time.monotonic() + self.config.timeout
        if not self._operation_lock.acquire(timeout=self.config.timeout):
            raise LspBusyError(
                f"timed out after {self.config.timeout:g}s waiting for the Lean LSP session"
            )
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LspBusyError(
                    f"timed out after {self.config.timeout:g}s waiting for the Lean LSP session"
                )
            return self._get_diagnostics(file_path, timeout=remaining)
        finally:
            self._operation_lock.release()

    def _get_diagnostics(self, file_path: str, *, timeout: float | None = None) -> list[dict]:
        path = Path(file_path).resolve()
        uri = path.as_uri()
        operation_timeout = self.config.timeout if timeout is None else timeout
        deadline = time.monotonic() + operation_timeout

        try:
            content = path.read_text()
        except Exception as e:
            return [{"severity": "error", "message": f"Cannot read file: {e}"}]

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
            timeout=self._remaining(deadline, operation_timeout),
        )

        try:
            # An empty published diagnostic list means the file is clean. No
            # publication at all is a timeout/error and must never be conflated
            # with that valid empty result.
            return self._collect_diagnostics(
                uri,
                timeout=self._remaining(deadline, operation_timeout),
            )
        finally:
            try:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._send_notification(
                        "textDocument/didClose",
                        {"textDocument": {"uri": uri}},
                        timeout=remaining,
                    )
            except Exception:
                logger.warning("failed to close LSP document %s", uri, exc_info=True)

    def hover(self, file_path: str, line: int, character: int) -> str | None:
        """Get hover information at a position."""
        deadline = time.monotonic() + self.config.timeout
        if not self._operation_lock.acquire(timeout=self.config.timeout):
            raise LspBusyError(
                f"timed out after {self.config.timeout:g}s waiting for the Lean LSP session"
            )
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LspBusyError(
                    f"timed out after {self.config.timeout:g}s waiting for the Lean LSP session"
                )
            return self._hover(file_path, line, character, timeout=remaining)
        finally:
            self._operation_lock.release()

    def _hover(
        self,
        file_path: str,
        line: int,
        character: int,
        *,
        timeout: float = 30,
    ) -> str | None:
        path = Path(file_path).resolve()
        content = path.read_text()
        uri = path.as_uri()
        deadline = time.monotonic() + timeout
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
            timeout=self._remaining(deadline, timeout),
        )
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
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._send_notification(
                        "textDocument/didClose",
                        {"textDocument": {"uri": uri}},
                        timeout=remaining,
                    )
            except Exception:
                logger.warning("failed to close LSP document %s", uri, exc_info=True)
        if result and "contents" in result:
            contents = result["contents"]
            if isinstance(contents, dict):
                return contents.get("value", "")
            return str(contents)
        return None

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
        params: dict,
        *,
        timeout: float = 30,
    ) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
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
            if msg and msg.get("id") == request_id:
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
        raise TimeoutError(
            f"timed out after {timeout:g}s waiting for LSP response {request_id}"
        )

    def _read_message(self, timeout: float = 5) -> dict | None:
        """Read one bounded JSON-RPC frame within one absolute deadline."""
        if not self.process or not self.process.stdout:
            return None

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
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
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

    def _collect_diagnostics(self, uri: str, timeout: float) -> list[dict]:
        """Collect diagnostic notifications for a URI."""
        diagnostics: list[dict] = []
        deadline = time.monotonic() + timeout
        received = False

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if received:
                    return diagnostics
                raise TimeoutError(
                    f"timed out after {timeout:g}s waiting for diagnostics for {uri}"
                )

            # Before the first publication, keep waiting all the way to the
            # configured deadline. After one arrives, a one-second quiet period
            # is enough to treat the latest publication as final, while still
            # respecting the same total deadline.
            read_timeout = min(1 if received else 2, remaining)
            msg = self._read_message(timeout=read_timeout)
            if msg is None:
                if received:
                    return diagnostics
                continue
            if msg.get("method") == "textDocument/publishDiagnostics":
                params = msg.get("params", {})
                if not isinstance(params, dict):
                    raise LspProtocolError(
                        "publishDiagnostics params must be an object"
                    )
                published = params.get("diagnostics")
                if not isinstance(published, list):
                    raise LspProtocolError(
                        "publishDiagnostics diagnostics must be a list"
                    )
                if params.get("uri") == uri:
                    received = True
                    diagnostics = published


class LeanLspProjects:
    """Lazily keep one language-server session per explicit Lean project."""

    def __init__(
        self,
        session_factory: Callable[[Path], LeanLspSession] | None = None,
    ) -> None:
        self._session_factory = session_factory or self._start_session
        self._sessions: dict[Path, LeanLspSession] = {}
        self._lock = threading.Lock()
        self._closed = False

    @staticmethod
    def _start_session(project_dir: Path) -> LeanLspSession:
        session = LeanLspSession(LspConfig(cwd=str(project_dir)))
        session.start()
        return session

    def get(self, project_dir: str) -> LeanLspSession:
        """Return the session for a validated absolute Lake project."""
        root = resolve_lean_project_dir(project_dir)
        with self._lock:
            if self._closed:
                raise RuntimeError("Lean LSP project router is closed")
            session = self._sessions.get(root)
            if session is None:
                session = self._session_factory(root)
                self._sessions[root] = session
            return session

    def close(self) -> None:
        """Close all sessions created by this router."""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._closed = True
        for session in sessions:
            session.close()


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
    from fastmcp.server import FastMCP

    server = FastMCP(name="autoform-lsp")

    @server.tool
    def lean_diagnostic_messages(project_dir: str, file_path: str) -> str:
        """Return Lean diagnostics for an in-project file.

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
