"""Coding agents the loop runs without a terminal: Claude Code and Codex.

The agent gets the prompt on standard input, works in the worktree, and writes
its report file; it gets no GitHub credentials. Its JSON event stream is kept as
the transcript, from which :func:`parse_usage` reads turns, tokens and cost. A
run cut short by a rate limit is resumed in the same session after a pause.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import lean_probe

BACKENDS = ("claude", "codex")
_RATE_LIMIT = re.compile(r"\b429\b|rate.?limit|too many requests|overloaded|quota exceeded", re.IGNORECASE)
_RESUME_PROMPT = (
    "Continue the task exactly where you stopped: your previous turn was cut short by an API rate limit. "
    "Do not start again from scratch."
)


class AgentError(RuntimeError):
    pass


class Stopped(Exception):
    """The operator asked the worker to stop while an agent ran."""


@dataclass(frozen=True, slots=True)
class AgentSpec:
    backend: str
    model: str | None = None
    effort: str | None = None

    @property
    def label(self) -> str:
        return ":".join(part for part in (self.backend, self.model or "", self.effort or "") if part)


def parse_agent_spec(text: str) -> AgentSpec:
    """``claude``, ``codex:MODEL`` or ``claude:MODEL:EFFORT``; an empty field keeps the agent's default."""

    backend, _, rest = text.strip().partition(":")
    model, _, effort = rest.partition(":")
    if backend not in BACKENDS or ":" in effort:
        raise ValueError(f"agent {text!r}: expected claude[:MODEL[:EFFORT]] or codex[:MODEL[:EFFORT]]")
    return AgentSpec(backend, model or None, effort or None)


@dataclass(frozen=True, slots=True)
class Price:
    """Dollars per million tokens, to estimate what a Codex run cost."""

    input: float
    cached_input: float
    output: float
    cache_write: float | None = None


def parse_price(text: str) -> Price:
    """``input=2.5,cached=0.25,output=10[,write=3]``: dollars per million tokens; cache writes cost ``input``
    unless ``write`` says otherwise."""

    values: dict[str, float] = {}
    for part in text.split(","):
        key, _, value = part.partition("=")
        try:
            values[key.strip()] = float(value)
        except ValueError:
            raise ValueError(f"price {text!r}: expected input=N,cached=N,output=N[,write=N]") from None
    if not {"input", "cached", "output"} <= set(values) <= {"input", "cached", "output", "write"} or min(
        values.values()
    ) < 0:
        raise ValueError(f"price {text!r}: expected input=N,cached=N,output=N[,write=N]")
    return Price(values["input"], values["cached"], values["output"], values.get("write"))


@dataclass(slots=True)
class Usage:
    backend: str
    model: str | None = None
    exit_status: int | None = None
    seconds: float = 0.0
    turns: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    session: str | None = None
    rate_limited: bool = False
    timed_out: bool = False
    error: str = ""
    segments: int = 0
    transcript: str = ""
    resumes: int = 0
    final_text: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend, "model": self.model, "exit_status": self.exit_status,
            "seconds": round(self.seconds), "turns": self.turns, "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens, "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd, "rate_limited": self.rate_limited, "timed_out": self.timed_out,
            "error": self.error[-500:], "resumes": self.resumes, "transcript": self.transcript,
        }


def agent_command(
    spec: AgentSpec,
    *,
    cwd: Path,
    resume: str | None = None,
    extra_args: Sequence[str] = (),
    budget_usd: float | None = None,
) -> list[str]:
    """The command line that runs ``spec`` without a terminal, reading its prompt from standard input."""

    if spec.backend == "claude":
        command = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
        if spec.model:
            command += ["--model", spec.model]
        if spec.effort:
            command += ["--effort", spec.effort]
        if budget_usd is not None:
            command += ["--max-budget-usd", f"{budget_usd:g}"]
        if resume:
            command += ["--resume", resume]
        return command + list(extra_args)
    command = ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check"]
    command += ["--cd", str(cwd)]
    if spec.model:
        command += ["--model", spec.model]
    if spec.effort:
        command += ["--config", f'model_reasoning_effort="{spec.effort}"']
    command += list(extra_args)
    return command + (["resume", resume, "-"] if resume else ["-"])


_CREDENTIAL_VARIABLES = (
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "SSH_AUTH_SOCK", "SSH_AGENT_PID",
    "GIT_ASKPASS", "SSH_ASKPASS", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
)


def agent_env(base: Mapping[str, str], *, scratch: Path) -> dict[str, str]:
    """The environment of the agent and of the checks run on its change: no GitHub token, an empty ``gh``
    configuration, no SSH agent, and Git reading neither the user's nor the system's configuration, so no
    credential helper either. This keeps credentials out of honest mistakes; it is not a sandbox."""

    env = {
        name: value for name, value in base.items()
        if name not in _CREDENTIAL_VARIABLES and not name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }
    empty = scratch / "no-gh"
    empty.mkdir(parents=True, exist_ok=True)
    gitconfig = scratch / "gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text("", encoding="utf-8")
    env.update(GH_CONFIG_DIR=str(empty), GIT_TERMINAL_PROMPT="0", GIT_CONFIG_GLOBAL=str(gitconfig),
               GIT_CONFIG_NOSYSTEM="1")
    env.setdefault("CLAUDE_CODE_MAX_RETRIES", "20")
    return env


def parse_usage(backend: str, transcript: Path, price: Price | None = None) -> Usage:
    """Turns, tokens, cost and session of every session segment in a transcript.

    Claude Code reports a list-price cost that is cumulative over a resumed
    session, so the largest figure is taken. Codex reports tokens only; with a
    ``price`` they are turned into dollars, else the cost stays unknown.
    """

    usage = Usage(backend=backend, transcript=str(transcript))
    try:
        text = transcript.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return usage
    codex_failed = ""
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if backend == "claude" and kind == "result":
            tokens = event.get("usage") or {}
            usage.segments += 1
            cost = event.get("total_cost_usd")
            if isinstance(cost, (int, float)):
                usage.cost_usd = round(max(usage.cost_usd or 0.0, float(cost)), 4)
            usage.turns += int(event.get("num_turns") or 0)
            usage.input_tokens += int(tokens.get("input_tokens") or 0) + int(tokens.get("cache_creation_input_tokens") or 0)
            usage.cached_input_tokens += int(tokens.get("cache_read_input_tokens") or 0)
            usage.output_tokens += int(tokens.get("output_tokens") or 0)
            usage.session = event.get("session_id") or usage.session
            status = event.get("api_error_status")
            result_text = str(event.get("result") or "")
            usage.rate_limited = status == 429 or bool(event.get("is_error") and _RATE_LIMIT.search(result_text))
            usage.error = result_text[-1000:] if event.get("is_error") else ""
            usage.final_text = result_text
        elif backend == "codex":
            if kind == "thread.started":
                usage.session = event.get("thread_id") or usage.session
                usage.segments += 1
                codex_failed = ""
            elif kind == "turn.completed":
                tokens = event.get("usage") or {}
                usage.turns += 1
                usage.input_tokens += int(tokens.get("input_tokens") or 0)
                usage.cached_input_tokens += int(tokens.get("cached_input_tokens") or 0)
                usage.cache_write_tokens += int(tokens.get("cache_write_input_tokens") or 0)
                usage.output_tokens += int(tokens.get("output_tokens") or 0)
                codex_failed = ""
            elif kind == "item.completed" and (event.get("item") or {}).get("type") == "agent_message":
                usage.final_text = str((event.get("item") or {}).get("text") or "")
            elif kind in ("error", "turn.failed"):
                message = str(event.get("message") or (event.get("error") or {}).get("message") or event.get("error"))
                if kind == "turn.failed" or "retrying" not in message.lower():
                    codex_failed = message
    if backend == "codex":
        usage.rate_limited = bool(codex_failed and _RATE_LIMIT.search(codex_failed))
        usage.error = codex_failed[-1000:]
        if price is not None:
            fresh = max(0, usage.input_tokens - usage.cached_input_tokens - usage.cache_write_tokens)
            write = price.input if price.cache_write is None else price.cache_write
            usage.cost_usd = round(
                (fresh * price.input + usage.cached_input_tokens * price.cached_input + usage.cache_write_tokens * write
                 + usage.output_tokens * price.output) / 1e6,
                4,
            )
    return usage


class AgentRunner:
    """Runs agents one at a time for a worker, and stops the running one when asked."""

    def __init__(self, *, env: Mapping[str, str], scratch: Path, extra_args: Mapping[str, Sequence[str]] | None = None,
                 price: Price | None = None, max_resumes: int = 4, resume_pause: float = 180.0,
                 sleep: Callable[[float], object] | None = None) -> None:
        self.env = agent_env(env, scratch=scratch)
        self.extra_args = {backend: tuple(args) for backend, args in (extra_args or {}).items()}
        self.price = price
        self.max_resumes = max_resumes
        self.resume_pause = resume_pause
        self._wake = threading.Event()
        # The pause before resuming a rate-limited session ends early when the worker is stopped.
        self.sleep = sleep or self._wake.wait
        self._session: lean_probe.Session | None = None
        # Reentrant: the stop request can come from a signal handler while this thread holds it.
        self._lock = threading.RLock()
        self._stopping = False

    def stop(self) -> None:
        """Stop the running agent and everything it started; later runs raise :class:`Stopped`."""

        with self._lock:
            self._stopping = True
            session = self._session
        self._wake.set()
        if session is not None:
            lean_probe.end_session(session)

    def run(self, spec: AgentSpec, prompt: str, *, cwd: Path, transcript: Path, timeout: float,
            budget_usd: float | None = None) -> Usage:
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.with_suffix(".prompt.md").write_text(prompt, encoding="utf-8")
        start = time.monotonic()
        session: str | None = None
        text = prompt
        usage = Usage(backend=spec.backend, model=spec.model, transcript=str(transcript))
        for attempt in range(self.max_resumes + 1):
            if self._stopping:
                raise Stopped
            left = timeout - (time.monotonic() - start)
            if left <= 0:
                usage.timed_out = True
                break
            command = agent_command(spec, cwd=cwd, resume=session, extra_args=self.extra_args.get(spec.backend, ()),
                                    budget_usd=budget_usd)
            status = self._run_once(command, text, cwd=cwd, transcript=transcript, timeout=left)
            usage = parse_usage(spec.backend, transcript, self.price)
            usage.model, usage.exit_status, usage.resumes = spec.model, status, attempt
            if self._stopping:
                raise Stopped
            if status is None:
                usage.timed_out = True
                break
            if not usage.rate_limited or not usage.session or attempt == self.max_resumes:
                break
            session, text = usage.session, _RESUME_PROMPT
            self.sleep(min(self.resume_pause * (attempt + 1), max(0.0, timeout - (time.monotonic() - start))))
        usage.seconds = time.monotonic() - start
        return usage

    def _run_once(self, command: list[str], text: str, *, cwd: Path, transcript: Path, timeout: float) -> int | None:
        stderr_path = transcript.with_suffix(".stderr")
        with open(transcript, "a", encoding="utf-8") as out, open(stderr_path, "a", encoding="utf-8") as err:
            try:
                session = lean_probe.start_session(command, cwd=cwd, env=self.env, stdin=subprocess.PIPE, stdout=out,
                                                   stderr=err)
            except OSError as error:
                raise AgentError(f"cannot run {command[0]}: {error}") from error
            process = session.process
            with self._lock:
                self._session = session
            try:
                try:
                    # One deadline covers handing over the prompt and the run.
                    process.communicate(input=text, timeout=timeout)
                except subprocess.TimeoutExpired:
                    return None
                except BrokenPipeError:
                    process.wait(timeout=timeout)
                return process.returncode
            finally:
                # Also ends whatever the agent left running, such as a background shell.
                lean_probe.end_session(session)
                with self._lock:
                    self._session = None


def read_report(path: Path) -> dict[str, object]:
    """The agent's report, or a failed status saying why it cannot be read."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return {"status": "failed", "notes": f"no readable report at {path.name}: {error}"}
    if not isinstance(data, dict):
        return {"status": "failed", "notes": "the report is not a JSON object"}
    return data
