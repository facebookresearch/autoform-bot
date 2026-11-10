"""A resident Lean REPL for tactic attempts at the goal a `sorry` leaves.

One long-lived REPL process per project. It imports a file's header once and
keeps that environment. A tactic batch elaborates the text before the target's
declaration once (cached while it is unchanged), then the declaration with the
target line replaced by `sorry`, and runs each candidate as a REPL tactic step
on the goal that `sorry` left: milliseconds per candidate, where the language
server re-elaborates the file for each one.

The fast path only answers when its answer means what the language server's
would. The target line must hold a whole tactic step that `sorry` can stand in
for, and the declaration must elaborate with no error once that line is
`sorry`, so that a candidate which closes the goal is a candidate that makes
the declaration compile. Anything else raises :class:`ResidentFallback` and the
caller uses the language server. A candidate is `closed` when the REPL reports
``proofStatus: Completed`` (the kernel accepted the goal's proof term, with no
`sorry` or metavariable in it). That check runs on the goal alone and rejects
sound proofs of goals nested in a larger proof, so a candidate that leaves no
goals and no error but fails it is confirmed by elaborating the declaration
with the candidate written in.

The header is split from the text and checked once per distinct header text
with Lean's own header parser (``parse_header.lean``), so a header Lean reads
differently, or a `module` or `prelude` header, falls back. Its environment is
keyed on the `.olean` files of every project module the file imports, directly
or not, so a rebuild of any of them starts a fresh process. A module whose
source changed since its last build is used as built and named in the result,
as `lean_diagnostic_messages` reports stale imports rather than building them.

The REPL runs requests one at a time and cannot cancel one, so a candidate that
overruns its budget costs the process: it is killed, and the next request
starts a fresh one. A header or declaration still loading at the deadline is
left to finish instead, and the next call collects it
(:class:`ResidentStillLoading`).
"""

from __future__ import annotations

import json
import os
import re
import select
import subprocess
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path
from typing import Any

import psutil

from servers._process import _kill_subprocesses, inherit_clean_env
from servers.repl.core import run_header_parser

logger = getLogger(__name__)

# Prefixes and declarations kept elaborated per process. Each holds its proof states inside the
# REPL, which has no way to free them, so the process is also restarted once it
# has accumulated MAX_STATES of them or passed its memory ceiling.
MAX_CACHED = 4
MAX_STATES = 5000
DEFAULT_MAX_RSS_GB = 8.0
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
STDERR_TAIL_BYTES = 2000
KILL_SECONDS = 5.0
HEADER_CHECK_SECONDS = 60.0
HEADER_CHECK_OUTPUT_BYTES = 1024 * 1024
MAX_CHECKED_HEADERS = 16
# Past this multiple of the memory ceiling a batch stops early; below it the
# check waits for the batch to end, since a restart mid-batch costs the rest.
HARD_RSS_FACTOR = 1.5
# A batch reads the process's memory at most this often.
RSS_CHECK_SECONDS = 1.0
STALE_NAMED = 3

_LEAN_ERROR_PREFIX = "Lean error:"


class ResidentFallback(Exception):
    """The fast path cannot answer for this line; use the language server. The message says why."""


class ResidentStillLoading(TimeoutError):
    """The REPL is still importing or elaborating this file; the next call continues it."""


class ResidentReplError(RuntimeError):
    """The REPL process failed; it has been stopped and the next call starts a fresh one."""


@dataclass(frozen=True)
class _Split:
    header: str
    header_lines: int
    imports: tuple[str, ...]
    body_lines: list[str]
    # Everything before the first command: the header with its comments, which
    # is what the header parser is shown.
    preamble: str
    # The first command, if it is one only a header may hold.
    header_keyword: str | None


_HEADER_KEYWORDS = re.compile(r"(module|prelude|public import|meta import|private import)\b")


def split_header(text: str) -> _Split:
    """Split a file into its import header and the rest.

    The header ends at the last `import` line before the first line that is
    neither an import, blank, nor a comment. A file this misreads (a `module`
    or `prelude` header, say) leaves an import after the header, which then
    fails to elaborate, which falls back to the language server.
    """
    lines = text.splitlines(keepends=True)
    last_import = -1
    depth = 0
    stop = len(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if depth:
            depth = max(0, depth + stripped.count("/-") - stripped.count("-/"))
            continue
        if stripped.startswith("import "):
            last_import = index
        elif stripped.startswith("/-"):
            depth = max(0, stripped.count("/-") - stripped.count("-/"))
        elif stripped and not stripped.startswith("--"):
            stop = index
            break
    header_lines = last_import + 1
    imports = tuple(
        line.split()[1]
        for line in lines[:header_lines]
        if line.strip().startswith("import ") and len(line.split()) > 1
    )
    keyword = None
    if stop < len(lines):
        match = _HEADER_KEYWORDS.match(lines[stop].strip())
        keyword = match.group(1) if match else None
    return _Split(
        "".join(lines[:header_lines]),
        header_lines,
        imports,
        lines[header_lines:],
        "".join(lines[:stop]),
        keyword,
    )


def _source_dirs(root: Path) -> list[Path]:
    """Where the project's own modules live: the root and each `srcDir` its lakefile names."""
    dirs = [root]
    for name in ("lakefile.toml", "lakefile.lean"):
        try:
            text = (root / name).read_text(encoding="utf-8")
        except OSError:
            continue
        for found in re.findall(r"srcDir\s*:?=\s*\"([^\"]+)\"", text):
            path = root / found
            if path.is_dir() and path not in dirs:
                dirs.append(path)
    return dirs


@dataclass(frozen=True)
class _BuildState:
    # Changes whenever any module below the header is rebuilt.
    key: tuple[Any, ...]
    # Project modules whose source is newer than their `.olean`.
    stale: tuple[str, ...]


def _is_term_position(lines: list[str], line: int) -> bool:
    """Whether the line follows a bare `:=`, where a tactic would need `by`."""
    for previous in reversed(lines[:line]):
        stripped = previous.split("--")[0].strip()
        if stripped:
            return stripped.endswith(":=")
    return False


def _top_level(line: str) -> bool:
    """A line that starts a command: text at column 0 that is not a line comment."""
    return bool(line.strip()) and not line[0].isspace() and not line.startswith("--")


def declaration_bounds(lines: list[str], index: int) -> tuple[int, int]:
    """The ``[start, end)`` lines of the command holding line ``index``.

    It starts at the nearest column-0 line at or above ``index``, together with
    the attributes, doc comment, and `... in` modifiers just above it, and ends
    before the next column-0 line. A file this misjudges elaborates the slice with an error,
    which falls back to the language server.
    """
    start = index
    while start > 0 and not _top_level(lines[start]):
        start -= 1
    while start > 0:
        above = lines[start - 1]
        if above.startswith("@[") or (_top_level(above) and above.rstrip().endswith(" in")):
            # An attribute, or a modifier such as `open Classical in` that
            # scopes the command below it.
            start -= 1
        elif above.rstrip().endswith("-/"):
            opening = start - 1
            while opening > 0 and "/-" not in lines[opening]:
                opening -= 1
            start = opening
        else:
            break
    end = index + 1
    while end < len(lines) and not _top_level(lines[end]):
        end += 1
    return start, end


def _first_line(text: str, limit: int = 200) -> str:
    text = text.removeprefix(_LEAN_ERROR_PREFIX).strip()
    return " ".join(text.split())[:limit]


def _errors(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in response.get("messages") or [] if m.get("severity") == "error"]


def _uses_sorry(response: dict[str, Any]) -> bool:
    # Lean has quoted it both ways across releases.
    markers = ("declaration uses `sorry`", "declaration uses 'sorry'")
    return any(marker in str(m.get("data", "")) for m in response.get("messages") or [] for marker in markers)


def classify_step(response: dict[str, Any]) -> dict[str, str] | None:
    """Say what one tactic step achieved, in the language server tool's outcomes.

    ``None`` means the step left no goals and no error but the REPL's own check
    of the proof term was inconclusive: that check runs on the goal alone, and
    rejects sound proofs of goals nested in a larger proof, so the caller
    confirms such a step by elaborating the declaration.
    """
    if "message" in response and "proofState" not in response:
        return {"outcome": "error", "detail": _first_line(str(response["message"]))}
    errors = _errors(response)
    if errors:
        return {"outcome": "error", "detail": _first_line(str(errors[0].get("data", "")))}
    status = str(response.get("proofStatus", ""))
    goals = response.get("goals") or []
    if goals:
        return {"outcome": "error", "detail": f"unsolved goals {_first_line(str(goals[0]))}".strip()}
    if status == "Completed":
        return {"outcome": "closed", "detail": ""}
    if status.startswith("Incomplete: contains sorry") or _uses_sorry(response):
        return {"outcome": "incomplete", "detail": "no error, but the proof still uses sorry"}
    return None


def classify_declaration(response: dict[str, Any]) -> dict[str, str]:
    """What the declaration does with a candidate written in: the language server's verdict on it."""
    errors = _errors(response)
    if errors:
        return {"outcome": "error", "detail": _first_line(str(errors[0].get("data", "")))}
    if _uses_sorry(response):
        return {"outcome": "incomplete", "detail": "no error, but the declaration still uses sorry"}
    return {"outcome": "closed", "detail": "confirmed by elaborating the declaration"}


class _StderrTail:
    """Drain a process's stderr on a thread, keeping only its last bytes."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._tail = b""
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._drain, name="resident-repl-stderr", daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        fd = self._stream.fileno()
        while True:
            try:
                chunk = os.read(fd, 1 << 16)
            except OSError:
                return
            if not chunk:
                return
            with self._lock:
                self._tail = (self._tail + chunk)[-STDERR_TAIL_BYTES:]

    def text(self) -> str:
        with self._lock:
            return self._tail.decode("utf-8", errors="replace").strip()

    def close(self) -> None:
        self._thread.join(timeout=1.0)
        try:
            self._stream.close()
        except OSError:
            pass


@dataclass(frozen=True)
class _Target:
    """One tactic line, located in the header / prefix / declaration split."""

    header: str
    header_key: Any
    header_check: tuple[str, ...]
    stale: tuple[str, ...]
    prefix: str
    declaration: list[str]
    index: int
    first_line: int
    indent: str
    newline: str

    def with_line(self, text: str) -> str:
        lines = list(self.declaration)
        lines[self.index] = f"{self.indent}{text}{self.newline}"
        return "".join(lines)


class ResidentRepl:
    """One project's resident REPL. Thread-safe; calls run one at a time."""

    def __init__(
        self,
        project_dir: Path,
        repl_command: list[str],
        *,
        header_command: list[str],
        max_rss_bytes: int = int(DEFAULT_MAX_RSS_GB * 1024**3),
    ) -> None:
        self.root = Path(project_dir)
        self.repl_command = list(repl_command)
        self.header_command = list(header_command)
        self.max_rss_bytes = max_rss_bytes
        self.closed = False
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._stderr: _StderrTail | None = None
        # Processes whose group could not be verified gone. No new process
        # starts, and close() does not return, until they are.
        self._unreaped: list[subprocess.Popen] = []
        self._buffer = b""
        self._source_dirs = _source_dirs(self.root)
        # A header's import lines -> its imports as Lean reads them, or why the fast path refuses it.
        self._check_lock = threading.Lock()
        self._checked_headers: OrderedDict[tuple[str, ...], tuple[str, ...] | str] = OrderedDict()
        self._header_checks: dict[tuple[str, ...], threading.Thread] = {}
        # Source path -> (mtime, imports), so an unchanged module is not read again.
        self._module_imports: dict[Path, tuple[int, tuple[str, ...]]] = {}
        # Requests sent whose responses have not been read, oldest first.
        self._outstanding: deque[tuple[str, Any]] = deque()
        self._header_key: Any = None
        self._header_env: int | None = None
        # Responses for the text before a declaration, and for a declaration
        # with its target line as `sorry`, most recent last.
        self._prefixes: OrderedDict[Any, dict[str, Any]] = OrderedDict()
        self._declarations: OrderedDict[Any, dict[str, Any]] = OrderedDict()
        self._states = 0
        self.restarts = 0

    # -- public ----------------------------------------------------------------

    def try_tactics(
        self,
        file_path: str,
        line: int,
        tactics: list[str],
        *,
        per_candidate_timeout: float,
        total_timeout: float,
    ) -> dict[str, Any]:
        """Try each candidate at the goal a `sorry` on ``line`` would leave.

        Returns the language server tool's result shape. Raises
        :class:`ResidentFallback` when that would not mean the same thing.
        """
        if os.name != "posix":
            raise ResidentFallback("the resident REPL requires a POSIX platform")
        deadline = time.monotonic() + total_timeout
        path = Path(file_path).resolve()
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        if line < 0 or line >= len(lines):
            return {"error": f"line {line} is outside a {len(lines)}-line file", "attempts": []}
        stripped = lines[line].rstrip("\r\n")
        target = self._locate(text, lines, line, deadline)

        attempts: list[dict[str, str]] = []
        with self._acquire(deadline):
            proof_state: int | None = self._prepare(target, deadline)
            rss_checked = time.monotonic()
            for tactic in tactics:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    attempts.append({"tactic": tactic, "outcome": "skipped", "detail": "overall budget exhausted"})
                    continue
                budget = min(per_candidate_timeout, remaining)
                if proof_state is None:
                    # A previous candidate overran and cost the process.
                    try:
                        proof_state = self._prepare(target, deadline)
                    except (ResidentStillLoading, ResidentReplError) as error:
                        attempts.append({"tactic": tactic, "outcome": "skipped", "detail": f"REPL restarting: {error}"})
                        continue
                try:
                    attempt = self._attempt(target, proof_state, tactic, budget, deadline)
                except ResidentStillLoading:
                    self._stop()
                    proof_state = None
                    attempt = {"outcome": "timeout", "detail": f"no result within {budget:.0f}s"}
                attempts.append({"tactic": tactic, **attempt})
                if proof_state is not None and time.monotonic() - rss_checked >= RSS_CHECK_SECONDS:
                    rss_checked = time.monotonic()
                    if self._rss() > HARD_RSS_FACTOR * self.max_rss_bytes:
                        logger.info("stopping resident REPL in %s mid-batch: memory", self.root)
                        self._stop()
                        proof_state = None
            self._enforce_limits()
        result = {"line": line, "replaced": stripped, "attempts": attempts, "backend": "repl"}
        if target.stale:
            named = ", ".join(target.stale[:STALE_NAMED])
            more = f" and {len(target.stale) - STALE_NAMED} more" if len(target.stale) > STALE_NAMED else ""
            result["note"] = (
                f"Changed since their last build, so the REPL used that build: {named}{more}. "
                "lean_diagnostic_messages reports them as stale imports."
            )
        return result

    def close(self) -> None:
        """Stop the process; raise RuntimeError, to be called again, while any group may still be live."""
        with self._lock:
            self.closed = True
            self._stop()
            self._reap_unreaped()
            with self._check_lock:
                checks = list(self._header_checks.values())
        # A check is bounded by its own deadline and stops its parser's group.
        for thread in checks:
            thread.join(HEADER_CHECK_SECONDS + KILL_SECONDS)
        with self._lock:
            if self._unreaped or any(thread.is_alive() for thread in checks):
                raise RuntimeError(f"a resident REPL process group in {self.root} has not exited")

    def stats(self) -> dict[str, Any]:
        return {
            "running": self._process is not None and self._process.poll() is None,
            "rss_bytes": self._rss(),
            "states": self._states,
            "declarations": len(self._declarations),
            "restarts": self.restarts,
            "unreaped": len(self._unreaped),
        }

    # -- preparation -----------------------------------------------------------

    def _locate(self, text: str, lines: list[str], line: int, deadline: float) -> _Target:
        stripped = lines[line].rstrip("\r\n")
        if not stripped.strip():
            raise ResidentFallback("the line is blank")
        if _is_term_position(lines, line):
            raise ResidentFallback("the line is a term after `:=`, not a tactic step")
        split = split_header(text)
        if line < split.header_lines:
            raise ResidentFallback("the line is in the import header")
        header_check = self._start_header_check(split)
        imports = tuple(module for module in split.imports if module != "Init")
        build = self._build_state(imports)
        body = split.body_lines
        index = line - split.header_lines
        start, end = declaration_bounds(body, index)
        if start == index:
            raise ResidentFallback("the line starts a command")
        return _Target(
            header=split.header,
            header_key=(str(self.root), split.header, build.key),
            header_check=header_check,
            stale=build.stale,
            prefix="".join(body[:start]),
            declaration=body[start:end],
            index=index - start,
            first_line=split.header_lines + start,
            indent=stripped[: len(stripped) - len(stripped.lstrip())],
            newline=lines[line][len(stripped):],
        )

    def _start_header_check(self, split: _Split) -> tuple[str, ...]:
        """Have Lean's header parser read this header, once per distinct list of import lines.

        The check runs on a thread beside the REPL's own import, which a new
        header pays anyway, so it adds no latency; `_await_header_check` waits
        for it before any candidate runs. Comments are left out of the key, so
        editing one does not run the parser again: an import Lean finds in what
        the split took for a comment reaches the REPL mid-file, fails, and
        falls back.
        """
        if split.header_keyword:
            raise ResidentFallback(f"the file has a `{split.header_keyword}` header")
        key = tuple(
            line.strip() for line in split.header.splitlines() if line.strip().startswith("import ")
        )
        lexical = tuple(module for module in split.imports if module != "Init")
        with self._check_lock:
            if key in self._checked_headers or key in self._header_checks:
                return key
            thread = threading.Thread(
                target=self._run_header_check,
                args=(key, split.preamble, lexical),
                name="resident-repl-header-check",
                daemon=True,
            )
            self._header_checks[key] = thread
        thread.start()
        return key

    def _run_header_check(self, key: tuple[str, ...], preamble: str, lexical: tuple[str, ...]) -> None:
        verdict: tuple[str, ...] | str | None
        try:
            analysis = run_header_parser(
                self.header_command,
                preamble,
                cwd=str(self.root),
                deadline=time.monotonic() + HEADER_CHECK_SECONDS,
                max_output_bytes=HEADER_CHECK_OUTPUT_BYTES,
            )
        except (TimeoutError, RuntimeError, ValueError, OSError) as error:
            logger.warning("header check failed in %s: %s", self.root, error)
            verdict = None
        else:
            if not analysis.accepts_leading_imports:
                verdict = "the file has a `module` or `prelude` header"
            elif analysis.modules != lexical:
                verdict = "Lean reads this header's imports differently from the resident REPL's split"
            else:
                verdict = lexical
        with self._check_lock:
            self._header_checks.pop(key, None)
            if verdict is None:
                # Not kept: the next call checks again.
                return
            self._checked_headers[key] = verdict
            while len(self._checked_headers) > MAX_CHECKED_HEADERS:
                self._checked_headers.popitem(last=False)

    def _await_header_check(self, key: tuple[str, ...], deadline: float) -> None:
        with self._check_lock:
            thread = self._header_checks.get(key)
        if thread is not None:
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                raise ResidentStillLoading("Lean's header parser is still reading the header; call again")
        with self._check_lock:
            verdict = self._checked_headers.get(key)
            if verdict is None:
                raise ResidentFallback("Lean's header parser could not check the header")
            self._checked_headers.move_to_end(key)
        if isinstance(verdict, str):
            raise ResidentFallback(verdict)

    def _module_source(self, module: str) -> Path | None:
        relative = Path(*module.split(".")).with_suffix(".lean")
        for directory in self._source_dirs:
            candidate = directory / relative
            if candidate.is_file():
                return candidate
        return None

    def _imports_of(self, source: Path, mtime: int) -> tuple[str, ...]:
        cached = self._module_imports.get(source)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            imports = split_header(source.read_text(encoding="utf-8")).imports
        except (OSError, UnicodeDecodeError):
            imports = ()
        self._module_imports[source] = (mtime, imports)
        return imports

    def _build_state(self, imports: tuple[str, ...]) -> _BuildState:
        """Walk the project modules below the header: their builds, and which are stale.

        Modules from packages are not walked; they change only when the
        packages do, which the manifest's modification time stands for.
        """
        build_dir = self.root / ".lake" / "build" / "lib" / "lean"
        oleans: list[tuple[str, int | None]] = []
        stale: list[str] = []
        seen: set[str] = set()
        pending = list(imports)
        while pending:
            module = pending.pop()
            if module in seen:
                continue
            seen.add(module)
            source = self._module_source(module)
            if source is None:
                continue
            try:
                source_mtime = source.stat().st_mtime_ns
            except OSError:
                continue
            try:
                olean_mtime: int | None = (build_dir / Path(*module.split(".")).with_suffix(".olean")).stat().st_mtime_ns
            except OSError:
                olean_mtime = None
            oleans.append((module, olean_mtime))
            if olean_mtime is None or olean_mtime < source_mtime:
                stale.append(module)
            pending.extend(self._imports_of(source, source_mtime))
        try:
            manifest: int | None = (self.root / "lake-manifest.json").stat().st_mtime_ns
        except OSError:
            manifest = None
        return _BuildState(key=(manifest, tuple(sorted(oleans, key=lambda item: item[0]))), stale=tuple(sorted(stale)))

    @contextmanager
    def _acquire(self, deadline: float) -> Iterator[None]:
        if not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise ResidentStillLoading("another tactic batch holds this project's REPL; call again")
        try:
            if self.closed:
                raise ResidentReplError("the resident REPL is closed")
            yield
        finally:
            self._lock.release()

    def _prepare(self, target: _Target, deadline: float) -> int:
        """Elaborate what precedes the target goal; return the proof state its `sorry` leaves."""
        if self._header_key != target.header_key:
            # One header environment per process: another file, or a rebuild
            # below this one, starts a fresh process.
            self._stop()
            self._header_key = target.header_key
        env = self._header(target, deadline)
        self._await_header_check(target.header_check, deadline)
        if target.prefix.strip():
            prefix_key = (target.header_key, target.prefix)
            prefix = self._cached(self._prefixes, prefix_key, "prefix", {"cmd": target.prefix, "env": env}, deadline)
            errors = _errors(prefix)
            if errors:
                raise ResidentFallback(
                    f"the file has an error before this declaration, at line "
                    f"{self._file_line(target, errors[0], before=True)}: "
                    f"{_first_line(str(errors[0].get('data', '')), 120)}"
                )
            env = int(prefix["env"])
        declaration_key = (target.header_key, target.prefix, target.with_line("sorry"))
        declaration = self._cached(
            self._declarations,
            declaration_key,
            "declaration",
            {"cmd": target.with_line("sorry"), "env": env},
            deadline,
        )
        errors = _errors(declaration)
        if errors:
            raise ResidentFallback(
                f"with the line as `sorry`, the declaration has an error at line "
                f"{self._file_line(target, errors[0])}: {_first_line(str(errors[0].get('data', '')), 120)}"
            )
        for sorry in declaration.get("sorries") or []:
            if sorry.get("pos", {}).get("line") == target.index + 1 and "proofState" in sorry:
                return int(sorry["proofState"])
        raise ResidentFallback("Lean reported no goal for a `sorry` on that line")

    def _header(self, target: _Target, deadline: float) -> int:
        if self._header_env is not None:
            return self._header_env
        response = self._request("header", target.header_key, {"cmd": target.header}, deadline - time.monotonic())
        errors = _errors(response)
        if errors or "env" not in response:
            detail = _first_line(str(errors[0].get("data", ""))) if errors else "no environment"
            self._stop()
            raise ResidentFallback(f"the REPL could not import the header: {detail}")
        self._header_env = int(response["env"])
        return self._header_env

    def _cached(
        self,
        cache: OrderedDict[Any, dict[str, Any]],
        key: Any,
        kind: str,
        payload: dict[str, Any],
        deadline: float,
    ) -> dict[str, Any]:
        response = cache.get(key)
        if response is None:
            response = self._request(kind, key, payload, deadline - time.monotonic())
        cache[key] = response
        cache.move_to_end(key)
        while len(cache) > MAX_CACHED:
            cache.popitem(last=False)
        return response

    def _attempt(
        self, target: _Target, proof_state: int, tactic: str, budget: float, batch_deadline: float
    ) -> dict[str, str]:
        """Run one candidate. Its budget bounds the tactic step; a confirmation,
        which costs what the language server pays for every candidate, may use
        what is left of the batch's."""
        response = self._request("tactic", None, {"tactic": tactic, "proofState": proof_state}, budget)
        self._states += 1
        outcome = classify_step(response)
        if outcome is None:
            env = self._header_env if not target.prefix.strip() else self._prefix_env(target)
            confirmed = self._request(
                "confirm", None, {"cmd": target.with_line(tactic), "env": env}, batch_deadline - time.monotonic()
            )
            self._states += 1
            outcome = classify_declaration(confirmed)
        elif outcome["outcome"] == "closed" and self._other_sorries(target):
            outcome = {"outcome": "incomplete", "detail": "closes this goal; the declaration has another sorry"}
        return outcome

    def _prefix_env(self, target: _Target) -> int:
        return int(self._prefixes[(target.header_key, target.prefix)]["env"])

    def _other_sorries(self, target: _Target) -> bool:
        declaration = self._declarations.get((target.header_key, target.prefix, target.with_line("sorry")))
        return bool(declaration) and len(declaration.get("sorries") or []) > 1

    @staticmethod
    def _file_line(target: _Target, message: dict[str, Any], *, before: bool = False) -> int | str:
        line = message.get("pos", {}).get("line")
        if not isinstance(line, int):
            return "?"
        # Zero-indexed, like the tools' `line` argument.
        offset = target.first_line - len(target.prefix.splitlines()) if before else target.first_line
        return offset + line - 1

    def _enforce_limits(self) -> None:
        rss = self._rss()
        if self._states > MAX_STATES or (rss and rss > self.max_rss_bytes):
            logger.info("restarting resident REPL in %s (states=%d rss=%d)", self.root, self._states, rss)
            self._stop()

    # -- transport -------------------------------------------------------------

    def _request(self, kind: str, key: Any, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Send a request (unless the same one is already outstanding) and wait for its response."""
        deadline = time.monotonic() + max(0.0, timeout)
        if (kind, key) not in self._outstanding or kind == "tactic":
            self._ensure_started()
            assert self._process is not None and self._process.stdin is not None
            try:
                self._process.stdin.write((json.dumps(payload) + "\n\n").encode("utf-8"))
                self._process.stdin.flush()
            except OSError as error:
                self._fail(f"could not write to the REPL: {error}")
            self._outstanding.append((kind, key))
        while True:
            response = self._read_response(deadline, kind)
            done_kind, done_key = self._outstanding.popleft()
            self._record(done_kind, done_key, response)
            if (done_kind, done_key) == (kind, key):
                return response

    def _record(self, kind: str, key: Any, response: dict[str, Any]) -> None:
        """File a response, including one a previous call stopped waiting for."""
        if kind == "header" and key == self._header_key and "env" in response:
            if not any(m.get("severity") == "error" for m in response.get("messages") or []):
                self._header_env = int(response["env"])
        elif kind == "prefix":
            self._prefixes[key] = response
            self._states += 1
        elif kind == "declaration":
            self._declarations[key] = response
            self._states += 1 + len(response.get("sorries") or [])

    def _read_response(self, deadline: float, kind: str) -> dict[str, Any]:
        assert self._process is not None and self._process.stdout is not None
        stdout = self._process.stdout
        while True:
            end = self._buffer.find(b"\n\n")
            if end >= 0:
                frame, self._buffer = self._buffer[:end], self._buffer[end + 2 :]
                if not frame.strip():
                    continue
                try:
                    response = json.loads(frame)
                except json.JSONDecodeError as error:
                    self._fail(f"the REPL sent malformed JSON: {error}")
                if not isinstance(response, dict):
                    self._fail("the REPL sent a response that is not an object")
                return response
            if len(self._buffer) > MAX_RESPONSE_BYTES:
                self._fail("the REPL response exceeded the size limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                what = {
                    "header": "importing the header",
                    "prefix": "elaborating the file up to this declaration",
                    "declaration": "elaborating this declaration",
                }.get(kind, "running")
                raise ResidentStillLoading(
                    f"the resident REPL is still {what}; call again and it continues where it stopped"
                )
            ready, _, _ = select.select([stdout], [], [], min(remaining, 1.0))
            if not ready:
                continue
            chunk = os.read(stdout.fileno(), 1 << 16)
            if not chunk:
                self._fail(f"the REPL exited{self._stderr_tail()}")
            self._buffer += chunk

    def _ensure_started(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        if self._process is not None:
            self._stop()
        self._reap_unreaped()
        if self._unreaped:
            raise ResidentFallback("an earlier resident REPL process has not exited yet")
        env = inherit_clean_env()
        self._process = subprocess.Popen(
            self.repl_command,
            cwd=self.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        self._stderr = _StderrTail(self._process.stderr)
        self.restarts += 1

    def _reap_unreaped(self) -> None:
        remaining = []
        for process in self._unreaped:
            try:
                _kill_subprocesses(process, process.pid, time.monotonic() + KILL_SECONDS)
            except RuntimeError:
                remaining.append(process)
        self._unreaped = remaining

    def _stop(self) -> None:
        """Stop the process and forget everything that lived in it."""
        process, self._process = self._process, None
        if process is not None:
            try:
                _kill_subprocesses(process, process.pid, time.monotonic() + KILL_SECONDS)
            except RuntimeError:
                logger.exception("failed to stop resident REPL in %s; retrying before the next start", self.root)
                self._unreaped.append(process)
            for stream in (process.stdin, process.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None
        self._buffer = b""
        self._outstanding.clear()
        self._header_env = None
        self._prefixes.clear()
        self._declarations.clear()
        self._states = 0

    def _fail(self, message: str):
        self._stop()
        raise ResidentReplError(message)

    def _stderr_tail(self) -> str:
        tail = self._stderr.text() if self._stderr is not None else ""
        return f": {tail}" if tail else ""

    def _rss(self) -> int:
        process = self._process
        if process is None or process.poll() is not None:
            return 0
        try:
            parent = psutil.Process(process.pid)
            members = [parent, *parent.children(recursive=True)]
            return sum(member.memory_info().rss for member in members)
        except psutil.Error:
            return 0
