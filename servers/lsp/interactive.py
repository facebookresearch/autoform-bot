"""Interactive Lean tools over the shared LSP session: goals, buffer checks, tactic attempts.

An extension of :class:`servers.lsp.server.LeanLspSession`. Hover and the
batch ``lean_diagnostic_messages`` keep their behaviour; this module adds
documents that stay open between calls, so repeated goal queries and tactic
attempts on one file reuse the import environment Lean's file worker already
holds instead of paying a full import load each time.

Everything runs under the base session's admission, timeout, and retirement
machinery. The only failures that leave the session in service are the ones
that cannot have desynchronised its stream: Lean still elaborating at the
deadline, another caller's buffer, and a well-formed JSON-RPC error response.
"""

from __future__ import annotations

import hashlib
import os
import select
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from typing import Any, NamedTuple, TypeVar

from fastmcp.server import FastMCP

from servers.lean_client import LeanRuntimeClient
from servers.lsp.server import (
    LeanLspSession,
    LspConfig,
    LspProtocolError,
    format_lsp_diagnostics,
)

logger = getLogger(__name__)
T = TypeVar("T")

# Documents held open per session. Each one pins a Lean file worker holding a
# full import environment, several gigabytes with Mathlib, so residency is
# bounded. One still makes repeated work on the same file nearly free, and
# alternating between files is no worse than reopening each time.
DEFAULT_LSP_MAX_OPEN_DOCUMENTS = 1
# Lean answers a request that a newer edit overtook with this code
# (`Lean/Data/JsonRpc.lean`, `Lean/Server/Requests.lean`). The request lost a
# race; nothing is broken.
LSP_CONTENT_MODIFIED = -32801
# The file worker for this document died. The watchdog restarts it on the next
# edit, but our view of the document is no longer the server's, so drop it.
LSP_WORKER_EXITED = -32901
# Budget for one candidate tactic. `grind` can run away, and a runaway
# candidate must cost one slot rather than the whole batch.
DEFAULT_TACTIC_CANDIDATE_TIMEOUT = 30.0
# Every candidate costs an elaboration and is spliced into a buffer that has to
# cross two 16 MB frames.
MAX_TACTIC_CANDIDATES = 8
MAX_TACTIC_LENGTH = 2000

# The fixed battery `lean_try_close` fires, cheapest and most specific first.
# Fixed rather than caller-supplied on purpose: composing a candidate list is
# itself work an agent has to decide to do, while this tool asks for nothing
# but a line.
CLOSER_BATTERY = ("aesop", "omega", "positivity", "linarith")
# Short, because the point is a cheap probe: the whole battery at 8s each is a
# worst case of about half a minute.
DEFAULT_CLOSER_CANDIDATE_TIMEOUT = 8.0

# Selects which Lean tools the LSP adapter exposes. A tool that is not
# registered does not appear in the agent's tool list at all, which is
# stronger than allowlisting it away.
TOOLSET_ENV = "AUTOFORM_LSP_TOOLSET"
BASELINE_TOOLS = ("lean_diagnostic_messages", "lean_hover")
FULL_TOOLS = BASELINE_TOOLS + (
    "lean_goal",
    "lean_check_buffer",
    "lean_try_tactics",
    "lean_try_close",
)
TOOLSETS = {"baseline": BASELINE_TOOLS, "full": FULL_TOOLS}
DEFAULT_TOOLSET = "full"

# Once a frame has started arriving it must be read to completion: abandoning
# it leaves the consumed bytes unrecoverable and every later read misparses the
# stream. A poll budget therefore bounds only the wait for a frame to start;
# this bounds the frame itself.
FRAME_READ_TIMEOUT = 30.0
# Lean can publish a trailing diagnostic either side of the progress
# notification that reports the file finished, so drain briefly after it.
PROGRESS_DONE_GRACE = 1.0
# Fallback for a server that publishes but never reports progress for the
# URI: treat this much silence as the end of the exchange.
PROGRESS_QUIET_FALLBACK = 5.0


def active_toolset() -> tuple[str, ...]:
    """The tool names this process should expose.

    An unrecognised value raises rather than falling back: silently serving the
    wrong surface would not look like a failure.
    """
    name = (os.environ.get(TOOLSET_ENV) or DEFAULT_TOOLSET).strip().lower()
    try:
        return TOOLSETS[name]
    except KeyError:
        raise ValueError(
            f"unknown {TOOLSET_ENV}={name!r}; expected one of: " + ", ".join(sorted(TOOLSETS))
        ) from None


class LspRequestFailed(RuntimeError):
    """One request got a well-formed JSON-RPC error response; the stream is still in sync."""

    def __init__(self, code: Any, message: str, data: Any = None) -> None:
        detail = f" ({data!r})" if data is not None else ""
        super().__init__(f"LSP request failed [{code}]: {message}{detail}")
        self.code = code
        self.lsp_message = message
        self.data = data


class LspStillElaborating(TimeoutError):
    """Lean has not finished the document; the session is healthy and keeps working."""


class LspBufferOwned(RuntimeError):
    """Another caller is holding a speculative buffer for this document.

    The session is shared, so the buffer is too. Two callers alternating edits
    on one document would each see the other's results, so the second is told.
    """


# Raised by an admitted operation without retiring the session.
_SESSION_PRESERVING = (LspStillElaborating, LspBufferOwned, LspRequestFailed)


class _Outcome(NamedTuple):
    value: Any
    error: BaseException | None


@dataclass
class _OpenDocument:
    """A document the server is holding open, and what we last sent it."""

    uri: str
    path: Path
    version: int
    text: str
    last_used: float
    # Set while the document holds text that is not what is on disk.
    dirty: bool = False
    # Who put it in that state, so a second caller is told rather than ignored.
    owner: str | None = None


@dataclass
class _UriState:
    """What the server has told us about one document, keyed by version.

    Notifications are recorded wherever they are read, including while hover
    waits for its response, so no publication is lost to whichever loop
    happened to consume the frame.
    """

    diagnostics: dict[int, list[dict]] = field(default_factory=dict)
    versionless_diagnostics: list[dict] | None = None
    progress_started: set[int] = field(default_factory=set)
    progress_empty_at: dict[int, float] = field(default_factory=dict)
    last_published_at: float = 0.0

    def prune(self, keep: int) -> None:
        """Drop bookkeeping older than the version now in flight.

        The newest diagnostics below ``keep`` survive: once Lean reports a later
        version complete without republishing, they still describe the document.
        """
        stale = sorted(version for version in self.diagnostics if version < keep)
        for version in stale[:-1]:
            del self.diagnostics[version]
        for version in [key for key in self.progress_empty_at if key < keep]:
            del self.progress_empty_at[version]
        self.progress_started = {version for version in self.progress_started if version >= keep}


class InteractiveLeanLspSession(LeanLspSession):
    """A Lean LSP session that can also hold documents open for goals and tactic attempts."""

    def __init__(
        self,
        config: LspConfig,
        *,
        max_open_documents: int = DEFAULT_LSP_MAX_OPEN_DOCUMENTS,
    ) -> None:
        super().__init__(config)
        self.max_open_documents = max_open_documents
        # Versions are allocated per session and never reused, so a frame left
        # over from an earlier operation is distinguishable from the one in
        # flight. They start above 1, the version upstream hover always opens.
        self._next_version = 1
        self._uri_state: dict[str, _UriState] = {}
        self._open: dict[str, _OpenDocument] = {}

    # -- public operations -------------------------------------------------

    def goal(self, file_path: str, line: int, character: int) -> dict:
        """Return the tactic goal at a position in the file as it is on disk."""
        path = Path(file_path).resolve()
        text = path.read_text(encoding="utf-8")
        out_of_range = _position_error(text, line, character)
        if out_of_range:
            return {"state": "invalid_position", "goals": [], "detail": out_of_range}
        return self._interactive(lambda remaining: self._goal(path, text, line, character, timeout=remaining))

    def check_buffer(self, file_path: str, text: str, owner: str) -> dict:
        """Diagnose caller-supplied text without writing anything to disk."""
        path = Path(file_path).resolve()
        return self._interactive(lambda remaining: self._check_buffer(path, text, owner, timeout=remaining))

    def try_tactics(
        self,
        file_path: str,
        line: int,
        tactics: list[str],
        owner: str,
        *,
        per_candidate_timeout: float,
        total_timeout: float,
    ) -> dict:
        """Try each candidate tactic in place of one line, reporting what each one does.

        ``total_timeout`` replaces the session timeout for this call and covers
        the wait to enter the session as well as every candidate.
        """
        path = Path(file_path).resolve()
        base_text = path.read_text(encoding="utf-8")
        lines = base_text.splitlines(keepends=True)
        if line < 0 or line >= len(lines):
            return {"error": f"line {line} is outside a {len(lines)}-line file", "attempts": []}
        return self._interactive(
            lambda remaining: self._try_tactics(
                path,
                base_text,
                lines,
                line,
                tactics,
                owner,
                per_candidate_timeout=per_candidate_timeout,
                timeout=remaining,
            ),
            timeout=total_timeout,
        )

    def _interactive(self, operation: Callable[[float], T], *, timeout: float | None = None) -> T:
        """Run under the base admission, but keep the session for request-scoped failures.

        The base session retires itself on any exception from an admitted
        operation. That is right for a timeout mid-frame, but a file that is
        still elaborating must keep its warm session so that a repeat call
        continues the same elaboration instead of restarting it from scratch.
        """

        def guarded(remaining: float) -> _Outcome:
            try:
                return _Outcome(operation(remaining), None)
            except _SESSION_PRESERVING as error:
                return _Outcome(None, error)

        outcome = self._admitted(guarded, timeout=timeout)
        if outcome.error is not None:
            raise outcome.error
        return outcome.value

    # -- operations, run with the session admitted -------------------------

    def _goal(self, path: Path, text: str, line: int, character: int, *, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        uri = path.as_uri()
        # Reading the goal resyncs the document to disk, which would discard a
        # speculative buffer another caller is holding.
        self._require_buffer_ownership(uri, None)
        version = self._sync_document(path, text, timeout=self._remaining(deadline, timeout))
        # Asking before this version has finished elaborating is what provokes
        # contentModified, so wait for the cycle rather than race it.
        self._collect_diagnostics(uri, version, timeout=self._remaining(deadline, timeout))
        return self._plain_goal(uri, line, character, timeout=self._remaining(deadline, timeout))

    def _check_buffer(self, path: Path, text: str, owner: str, *, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        uri = path.as_uri()
        self._require_buffer_ownership(uri, owner)
        version = self._sync_document(
            path, text, timeout=self._remaining(deadline, timeout), dirty=True, owner=owner
        )
        # Still elaborating keeps the buffer, so a repeat call continues it.
        diagnostics = self._collect_diagnostics(uri, version, timeout=self._remaining(deadline, timeout))
        self._restore_disk_text(path)
        return {
            "diagnostics": diagnostics,
            "buffer_sha256": _sha256(text),
            "disk_sha256": _file_sha256(path),
        }

    def _try_tactics(
        self,
        path: Path,
        base_text: str,
        lines: list[str],
        line: int,
        tactics: list[str],
        owner: str,
        *,
        per_candidate_timeout: float,
        timeout: float,
    ) -> dict:
        uri = path.as_uri()
        original = lines[line]
        stripped = original.rstrip("\r\n")
        indent = stripped[: len(stripped) - len(stripped.lstrip())]
        newline = original[len(stripped):]
        self._require_buffer_ownership(uri, owner)

        deadline = time.monotonic() + timeout
        attempts: list[dict] = []
        try:
            # The original text's holes, so that one in another declaration does
            # not count against a candidate. Unknown if it is still elaborating.
            original_sorries: int | None = None
            budget = min(per_candidate_timeout, deadline - time.monotonic())
            if budget > 0:
                version = self._sync_document(path, base_text, timeout=budget)
                try:
                    original_sorries = _sorry_count(self._collect_diagnostics(uri, version, timeout=budget))
                except LspStillElaborating:
                    pass
            for tactic in tactics:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    attempts.append({"tactic": tactic, "outcome": "skipped", "detail": "overall budget exhausted"})
                    continue
                budget = min(per_candidate_timeout, remaining)
                candidate_lines = list(lines)
                candidate_lines[line] = f"{indent}{tactic}{newline}"
                version = self._sync_document(path, "".join(candidate_lines), timeout=budget, dirty=True, owner=owner)
                try:
                    diagnostics = self._collect_diagnostics(uri, version, timeout=budget)
                except LspStillElaborating:
                    # The next edit supersedes this elaboration ($/cancelRequest
                    # does not reach didChange-driven work), so move on rather
                    # than give up a warm session over one expensive candidate.
                    detail = f"no result within {budget:.0f}s"
                    attempts.append({"tactic": tactic, "outcome": "timeout", "detail": detail})
                    continue
                attempts.append({"tactic": tactic, **_classify_attempt(diagnostics, original_sorries)})
        finally:
            # Never leave the document holding candidate text nobody asked for.
            # If this edit cannot be sent, the exception retires the session.
            if uri in self._open:
                self._sync_document(path, base_text, timeout=self.config.timeout)
        return {"line": line, "replaced": stripped, "attempts": attempts}

    def _restore_disk_text(self, path: Path) -> None:
        """Put the server's copy back to the file on disk, so the buffer is not left held."""
        document = self._open.get(path.as_uri())
        if document is None:
            return
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            self._close_open_document(document, timeout=self.config.timeout)
            return
        self._sync_document(path, text, timeout=self.config.timeout)

    def hover(self, file_path: str, line: int, character: int) -> str | None:
        """Hover as the base session does, but refuse a buffer another caller holds without retiring."""
        path = Path(file_path).resolve()
        content = path.read_text(encoding="utf-8")
        return self._interactive(lambda remaining: self._hover(path, content, line, character, timeout=remaining))

    def _hover(
        self,
        path: Path,
        content: str,
        line: int,
        character: int,
        *,
        timeout: float = 30,
    ) -> str | None:
        # Upstream hover opens and closes its own copy of the file. Lean must
        # never see a second didOpen for a document it is already holding, so
        # release ours first; the next goal or tactic call reopens it.
        deadline = time.monotonic() + timeout
        self._require_buffer_ownership(path.as_uri(), None)
        document = self._open.get(path.as_uri())
        if document is not None:
            self._close_open_document(document, timeout=self._remaining(deadline, timeout))
        return super()._hover(path, content, line, character, timeout=self._remaining(deadline, timeout))

    # -- document synchronisation ------------------------------------------

    def _sync_document(
        self,
        path: Path,
        text: str,
        *,
        timeout: float,
        dirty: bool = False,
        owner: str | None = None,
    ) -> int:
        """Bring the server's copy of ``path`` to ``text``; return its new version.

        Opening a document costs a full import load, tens of seconds for a
        Mathlib-heavy file, while editing one the worker already holds is
        cheap, because Lean restarts elaboration at the command that changed
        and the imports stay loaded.

        The version is bumped on every sync, even for identical text, so the
        completion predicate always has a fresh cycle to correlate against.
        """
        uri = path.as_uri()
        document = self._open.get(uri)
        self._next_version += 1
        version = self._next_version
        if document is None:
            self._evict_documents(timeout=timeout)
            # Anything recorded for this URI belongs to an earlier open.
            self._uri_state.pop(uri, None)
            self._state_for(uri)
            self._send_notification(
                "textDocument/didOpen",
                {"textDocument": {"uri": uri, "languageId": "lean4", "version": version, "text": text}},
                timeout=timeout,
            )
            self._open[uri] = _OpenDocument(
                uri=uri,
                path=path,
                version=version,
                text=text,
                last_used=time.monotonic(),
                dirty=dirty,
                owner=owner,
            )
            return version
        self._state_for(uri).prune(version)
        self._send_notification(
            "textDocument/didChange",
            {"textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]},
            timeout=timeout,
        )
        document.version = version
        document.text = text
        document.last_used = time.monotonic()
        document.dirty = dirty
        document.owner = owner
        return version

    def _evict_documents(self, *, timeout: float) -> None:
        """Close least-recently-used documents to make room for one more."""
        limit = max(1, self.max_open_documents)
        while len(self._open) >= limit:
            victim = min(self._open.values(), key=lambda document: document.last_used)
            self._close_open_document(victim, timeout=timeout)

    def _close_open_document(self, document: _OpenDocument, *, timeout: float) -> None:
        self._open.pop(document.uri, None)
        self._uri_state.pop(document.uri, None)
        self._send_notification("textDocument/didClose", {"textDocument": {"uri": document.uri}}, timeout=timeout)

    def _require_buffer_ownership(self, uri: str, owner: str | None) -> None:
        """Refuse to touch a document holding another caller's speculative text.

        ``owner=None`` is a caller that only reads the file as it is on disk.
        """
        document = self._open.get(uri)
        if document is None or not document.dirty or document.owner is None or document.owner == owner:
            return
        raise LspBufferOwned(
            f"{uri} is holding a speculative buffer for {document.owner!r}; {owner!r} must not overwrite it"
        )

    def _state_for(self, uri: str) -> _UriState:
        state = self._uri_state.get(uri)
        if state is None:
            state = _UriState()
            self._uri_state[uri] = state
        return state

    # -- reading the stream ------------------------------------------------

    def _read_message(self, timeout: float = 5, finish_by: float | None = None) -> dict | None:
        message = super()._read_message(timeout=timeout, finish_by=finish_by)
        if message is not None and "method" in message:
            self._record_notification(message)
        return message

    def _record_notification(self, msg: dict) -> None:
        """Route a diagnostics or file-progress notification into the per-document record."""
        method = msg.get("method")
        params = msg.get("params")
        if method == "textDocument/publishDiagnostics":
            if not isinstance(params, dict):
                raise LspProtocolError("publishDiagnostics params must be an object")
            published = params.get("diagnostics")
            if not isinstance(published, list):
                raise LspProtocolError("publishDiagnostics diagnostics must be a list")
            uri = params.get("uri")
            if not isinstance(uri, str):
                return
            state = self._state_for(uri)
            state.last_published_at = time.monotonic()
            version = params.get("version")
            if isinstance(version, int) and not isinstance(version, bool):
                state.diagnostics[version] = published
            else:
                state.versionless_diagnostics = published
        elif method == "$/lean/fileProgress":
            if not isinstance(params, dict):
                return
            document = params.get("textDocument")
            processing = params.get("processing")
            if not isinstance(document, dict) or not isinstance(processing, list):
                return
            uri = document.get("uri")
            version = document.get("version")
            if not isinstance(uri, str) or not isinstance(version, int) or isinstance(version, bool):
                return
            state = self._state_for(uri)
            if processing:
                state.progress_started.add(version)
                state.progress_empty_at.pop(version, None)
            else:
                state.progress_empty_at[version] = time.monotonic()

    def _pump(self, timeout: float) -> dict | None:
        """Wait up to ``timeout`` for a frame to start, then read all of it.

        The base reader abandons a frame cut by its deadline, which is fine
        for a call that retires the session on timeout. Diagnostics collection
        polls in short slices and must survive its deadline, so it waits for
        the first byte here and gives the frame itself ``FRAME_READ_TIMEOUT``.
        """
        if not self.process or not self.process.stdout:
            raise LspProtocolError("LSP process not running")
        ready, _, _ = select.select([self.process.stdout.fileno()], [], [], max(0.0, timeout))
        if not ready:
            return None
        return self._read_message(timeout=FRAME_READ_TIMEOUT)

    def _request(self, method: str, params: dict, *, timeout: float) -> Any:
        """Send a request whose error responses stay request-scoped."""
        deadline = time.monotonic() + timeout
        with self._lock:
            self._request_id += 1
            request_id = self._request_id
            self._write_message(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                timeout=self._remaining(deadline, timeout),
            )
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out after {timeout:g}s waiting for LSP response {request_id}")
                msg = self._pump(remaining)
                # Lean's workers send their own requests with numeric ids, so a
                # frame that names a method is never the response to ours.
                if msg is not None and "method" not in msg and msg.get("id") == request_id:
                    return _unwrap_response(request_id, msg)

    def _plain_goal(self, uri: str, line: int, character: int, *, timeout: float, retry: bool = True) -> dict:
        """Ask for the tactic goal, distinguishing every outcome Lean can give.

        ``null`` means the position is not inside a tactic block, which is not
        an error. An empty ``goals`` list means every goal there is closed.
        """
        try:
            result = self._request(
                "$/lean/plainGoal",
                {"textDocument": {"uri": uri}, "position": {"line": line, "character": character}},
                timeout=timeout,
            )
        except LspRequestFailed as error:
            if error.code == LSP_WORKER_EXITED:
                self._open.pop(uri, None)
                self._uri_state.pop(uri, None)
                return {"state": "worker_exited", "goals": [], "detail": error.lsp_message}
            if error.code == LSP_CONTENT_MODIFIED and retry:
                return self._plain_goal(uri, line, character, timeout=timeout, retry=False)
            if error.code == LSP_CONTENT_MODIFIED:
                return {"state": "content_changed", "goals": [], "detail": error.lsp_message}
            raise
        if result is None:
            return {"state": "no_tactic_goal", "goals": []}
        if not isinstance(result, dict):
            raise LspProtocolError(f"plainGoal returned a non-object result: {result!r}")
        goals = result.get("goals")
        if goals is not None and not isinstance(goals, list):
            raise LspProtocolError("plainGoal goals must be a list")
        goals = list(goals or [])
        return {"state": "closed" if not goals else "open", "goals": goals, "rendered": result.get("rendered")}

    def _collect_diagnostics(self, uri: str, version: int, timeout: float) -> list[dict]:
        """Collect the diagnostics Lean publishes once it has finished ``version``.

        Lean publishes twice for one ``didOpen``: first while Lake sets the
        file up, cleared to an empty list when setup ends, then the result of
        elaborating the source, tens of seconds later for a Mathlib-heavy file.
        A lull after the first burst is therefore not an answer.

        Completion comes from ``$/lean/fileProgress``, whose empty
        ``processing`` list means nothing is left for that version. On a warm
        worker that empty progress can arrive just before the publication it
        belongs to, so it counts only with evidence that this version was
        worked: a publication for it, or an earlier non-empty progress.

        At the deadline this raises :class:`LspStillElaborating` rather than
        returning what was published so far, which may be the empty list that
        ends import setup before any declaration was elaborated.
        """
        state = self._state_for(uri)
        entered_at = time.monotonic()
        deadline = entered_at + timeout
        completed_at: float | None = None

        while True:
            now = time.monotonic()
            worked = version in state.diagnostics or version in state.progress_started
            ready = state.progress_empty_at.get(version) is not None and worked
            if not ready:
                completed_at = None
            elif completed_at is None:
                completed_at = now
            if completed_at is not None and now - completed_at >= PROGRESS_DONE_GRACE:
                return self._diagnostics_for(uri, state, version)
            if now >= deadline:
                raise LspStillElaborating(
                    f"Lean has not finished elaborating {uri} after {timeout:g}s, so "
                    "there is no result yet (this is not a clean file). Call again to "
                    "keep waiting: the file stays open and Lean keeps elaborating it."
                )
            # A server that publishes but never reports progress for this URI
            # must not hold the caller until its whole deadline expires.
            if (
                version not in state.progress_started
                and version not in state.progress_empty_at
                and state.last_published_at > entered_at
                and now - state.last_published_at >= PROGRESS_QUIET_FALLBACK
            ):
                return self._diagnostics_for(uri, state, version)

            self._pump(timeout=min(1.0, deadline - now))

    @staticmethod
    def _diagnostics_for(uri: str, state: _UriState, version: int) -> list[dict]:
        """Return the diagnostics that describe ``version``, never an invented empty list.

        Falling back to the newest earlier version is sound only because Lean
        has already reported ``version`` complete: a reused snapshot
        republishes nothing because the previous answer still holds.
        """
        if version in state.diagnostics:
            return state.diagnostics[version]
        if state.versionless_diagnostics is not None:
            return state.versionless_diagnostics
        earlier = [key for key in state.diagnostics if key < version]
        if earlier:
            return state.diagnostics[max(earlier)]
        raise LspProtocolError(
            f"Lean reported {uri} complete at version {version} without ever publishing diagnostics for it"
        )


def _unwrap_response(request_id: int, msg: dict) -> Any:
    """Return a response's result; a well-formed error is request-scoped, a malformed one is not."""
    if "error" in msg:
        error = msg["error"]
        if isinstance(error, dict):
            raise LspRequestFailed(
                error.get("code", "unknown"),
                error.get("message", "unspecified protocol error"),
                error.get("data"),
            )
        raise LspProtocolError(f"LSP response {request_id} returned malformed error: {error!r}")
    if "result" not in msg:
        raise LspProtocolError(f"LSP response {request_id} has neither result nor error")
    return msg["result"]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str | None:
    try:
        return _sha256(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def _position_error(text: str, line: int, character: int) -> str:
    """Reject a position outside the buffer without a server round trip."""
    lines = text.splitlines()
    if line < 0 or character < 0:
        return "line and character must be nonnegative"
    if line >= len(lines):
        return f"line {line} is past the end of a {len(lines)}-line file"
    if character > len(lines[line]):
        return f"character {character} is past the end of line {line}, which has {len(lines[line])} characters"
    return ""


def format_buffer_check(result: dict) -> str:
    """Render a buffer check in wording that cannot be read as a file result.

    Deliberately never emits the clean-file string of `format_lsp_diagnostics`:
    this tool reports on text that is not on disk.
    """
    diagnostics = result.get("diagnostics") or []
    header = (
        "Buffer check — this text was NOT written to disk.\n"
        f"buffer sha256={result.get('buffer_sha256')}\n"
        f"file-on-disk sha256={result.get('disk_sha256')}"
    )
    if not diagnostics:
        return f"{header}\nThe buffer elaborates with no diagnostics."
    return f"{header}\n{format_lsp_diagnostics(diagnostics)}"


def _first_message(diagnostics: list[dict]) -> str:
    for diagnostic in diagnostics:
        message = (diagnostic.get("message") or "").replace("\n", " ").strip()
        if message:
            return message[:200]
    return ""


def _is_sorry_warning(diagnostic: dict) -> bool:
    return "declaration uses `sorry`" in (diagnostic.get("message") or "")


def _sorry_count(diagnostics: list[dict]) -> int:
    """How many declarations use `sorry`: Lean warns once for each."""
    return sum(_is_sorry_warning(item) for item in diagnostics)


def _classify_attempt(diagnostics: list[dict], original_sorries: int | None) -> dict:
    """Say what a candidate achieved, without overstating it.

    A tactic that leaves goals behind produces an `unsolved goals` error. The
    case worth separating is a candidate with no error while the declaration
    still has a hole elsewhere: that is not a proof. The candidate edits one
    declaration, so it closed that one when no declaration uses `sorry` any
    more, or fewer than in the original text (*original_sorries*, None when
    unknown); a hole in another declaration does not count against it.
    """
    errors = [item for item in diagnostics if item.get("severity") == 1]
    if errors:
        return {"outcome": "error", "detail": _first_message(errors)}
    sorries = _sorry_count(diagnostics)
    if sorries and (original_sorries is None or sorries >= original_sorries):
        return {"outcome": "incomplete", "detail": "no error, but the declaration may still use sorry"}
    warnings = [item for item in diagnostics if item.get("severity") == 2 and not _is_sorry_warning(item)]
    return {"outcome": "closed", "detail": _first_message(warnings)}


def format_tactic_attempts(result: dict) -> str:
    """Render a batch of tactic attempts for the MCP tool response."""
    if result.get("error"):
        return f"Could not try tactics: {result['error']}"
    where = (
        "each run at the goal a `sorry` there leaves, in the resident REPL; NOT written to disk."
        if result.get("backend") == "repl"
        else "buffer only, NOT written to disk."
    )
    lines = [
        f"Tactic attempts at line {result['line']} — {where}",
        f"replacing: {result['replaced'].strip()!r}",
        "",
    ]
    for attempt in result.get("attempts", []):
        detail = f"  — {attempt['detail']}" if attempt.get("detail") else ""
        lines.append(f"  {attempt['outcome']:<10} {attempt['tactic']}{detail}")
    closed = [attempt["tactic"] for attempt in result.get("attempts", []) if attempt["outcome"] == "closed"]
    lines.append("")
    lines.append(
        f"Closed with no errors and no sorry left in the edited declaration: {', '.join(closed)}"
        if closed
        else "None of these closed it."
    )
    lines.append(
        "Write the winner to the file and confirm with lean_diagnostic_messages; this result is not verification."
    )
    if result.get("note"):
        lines.append(result["note"])
    return "\n".join(lines)


def format_goal(result: dict) -> str:
    """Render a goal query for the MCP tool response."""
    state = result.get("state")
    if state == "no_tactic_goal":
        return "No tactic goal at this position (not inside a tactic block)."
    if state == "closed":
        return "No goals remaining at this position."
    if state in {"content_changed", "invalid_position", "worker_exited"}:
        return f"Could not read a goal ({state}): {result.get('detail', '')}".strip()
    goals = result.get("goals") or []
    rendered = result.get("rendered")
    if isinstance(rendered, str) and rendered.strip():
        return rendered
    return f"Goals ({len(goals)})\n" + "\n\n".join(str(goal) for goal in goals)


def register_interactive_tools(server: FastMCP, runtime: LeanRuntimeClient) -> None:
    """Register the interactive tools that `AUTOFORM_LSP_TOOLSET` selects on the LSP adapter."""
    selected = active_toolset()

    def tool(function: Callable[..., str]) -> Callable[..., str]:
        if function.__name__ in selected:
            server.tool(function)
        return function

    @tool
    def lean_goal(project_dir: str, file_path: str, line: int, character: int) -> str:
        """Return the Lean tactic goal at a zero-indexed position in a file.

        Reads the file from disk. Reports "no goals remaining" and "not inside a
        tactic block" differently, because they mean different things.

        Args:
            project_dir: Absolute path to the Lake project root.
            file_path: Absolute path, or a path relative to project_dir, to a Lean file.
            line: Zero-indexed line number.
            character: Zero-indexed character position.
        """
        return runtime.request(
            "lsp.goal",
            {"project_dir": project_dir, "file_path": file_path, "line": line, "character": character},
        )

    @tool
    def lean_check_buffer(project_dir: str, file_path: str, content: str, owner: str) -> str:
        """Diagnose candidate Lean text WITHOUT writing it to disk.

        For trying something out — a closing tactic, an alternative lemma —
        against a file the proof-phase gate will not let you edit freely. The
        result describes your text, not the file, and is not accepted as
        verification: only `lean_diagnostic_messages` reads the file itself.

        The file must already exist on disk; create it first if it is new.

        Args:
            project_dir: Absolute path to the Lake project root.
            file_path: Absolute path, or a path relative to project_dir, to a Lean file.
            content: The complete candidate text of the file.
            owner: The name your orchestrator gave you (your subagent name),
                which no concurrent caller shares, so another caller's
                speculative buffer is reported rather than overwritten.
        """
        return runtime.request(
            "lsp.check_buffer",
            {"project_dir": project_dir, "file_path": file_path, "content": content, "owner": owner},
        )

    @tool
    def lean_try_tactics(project_dir: str, file_path: str, line: int, tactics: list[str], owner: str) -> str:
        """Try each candidate tactic in place of one line, and report what each does.

        Replaces the whole content of `line`, keeping its indentation, once per
        candidate, and elaborates the result in memory. Nothing is written to
        disk. Intended for firing strong closing tactics at a goal cheaply —
        for example ["grind", "aesop", "omega", "decide", "simp_all"] at a line
        holding `sorry`.

        Outcomes: `closed` (no errors and no sorry left in the edited
        declaration; a hole in another declaration does not count), `incomplete`
        (no error, but the declaration may still have a hole), `error`,
        `timeout`, `skipped`. A tactic that leaves goals behind reports as
        `error`, because Lean reports unsolved goals as one.

        For anything other than whole-line replacement, use lean_check_buffer.

        Args:
            project_dir: Absolute path to the Lake project root.
            file_path: Absolute path, or a path relative to project_dir, to a Lean file.
            line: Zero-indexed line whose content each candidate replaces.
            tactics: Candidate tactics, at most 8.
            owner: The name your orchestrator gave you (your subagent name),
                which no concurrent caller shares, so another caller's
                speculative buffer is reported rather than overwritten.
        """
        return runtime.request(
            "lsp.try_tactics",
            {"project_dir": project_dir, "file_path": file_path, "line": line, "tactics": tactics, "owner": owner},
        )

    def lean_try_close(project_dir: str, file_path: str, line: int, owner: str) -> str:
        """Fire a standard set of closing tactics at one line and report what closes it.

        The first thing to reach for when a side goal is open. You name a line;
        it tries every closer below in place of that line, in memory, and tells
        you which ones closed the goal. Nothing is written to disk and the file
        is restored afterwards, so a miss costs nothing but the seconds.

        Reach for it *before* hand-writing a `have` for a routine side condition
        — a positivity fact, a linear-arithmetic or natural-number step. If one
        of these closes it, that is the proof.

        Tried, in order, at 8 seconds each:

            aesop, omega, positivity, linarith

        Outcomes per candidate: `closed`, `incomplete` (no error, but the
        declaration may still have a hole), `error`, `timeout`, `skipped`.

        Use `lean_try_tactics` instead when you want to choose the candidates
        yourself; this tool exists so that firing the standard set costs one
        call and no deliberation.

        Args:
            project_dir: Absolute path to the Lake project root.
            file_path: Absolute path, or a path relative to project_dir, to a Lean file.
            line: Zero-indexed line whose content each candidate replaces —
                normally the line holding `sorry` or the unfinished tactic.
            owner: The name your orchestrator gave you (your subagent name),
                which no concurrent caller shares, so another caller's
                speculative buffer is reported rather than overwritten.
        """
        return runtime.request(
            "lsp.try_tactics",
            {
                "project_dir": project_dir,
                "file_path": file_path,
                "line": line,
                "tactics": list(CLOSER_BATTERY),
                "owner": owner,
                "per_candidate_timeout": DEFAULT_CLOSER_CANDIDATE_TIMEOUT,
            },
        )

    tool(lean_try_close)
