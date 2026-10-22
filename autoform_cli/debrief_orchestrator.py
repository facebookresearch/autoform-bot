"""Orchestrator debrief: feedback on AutoformBot itself, once per Setup, Roadmap or Formalize run.

The agent that led the run asks ``autoform debrief orchestrator form`` for the
form, answers it, and stores the answer with ``autoform debrief orchestrator
record``. The form separates friction caused by the user's local setup from
friction with AutoformBot. When there is AutoformBot friction, ``record`` also
writes a draft issue for AutoformBot's tracker, with local paths replaced by
placeholders, and returns a link that opens it pre-filled. Nothing is sent: the
user reviews, edits, and submits the issue themselves.

Configuration (environment):

``AUTOFORM_DEBRIEF``
    On by default. ``0`` / ``false`` / ``no`` / ``off`` makes ``form`` say
    orchestrator debriefs are disabled, and the agent skips the step.
``AUTOFORM_DEBRIEF_DIR``
    The store, shared with subagent debriefs (see :func:`debrief.debrief_root`).

Store layout::

    runs/<run_id>.json      one RunRecord per run, schema_version 1
    issues/<run_id>.md      draft issue, written only when there is AutoformBot friction;
                            it ends with a link that opens the issue form pre-filled
"""

from __future__ import annotations

import importlib.metadata
import os
import platform
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from . import debrief_store as store
from .debrief import (
    MAX_ANSWER_BYTES,
    DebriefError,
    RecordError,
    _decode,
    _encode,
    _fields,
    _items,
    _text,
    debrief_root,
    parse_answer,
    project_dir,
)
from .lean import _git

SCHEMA_VERSION = 1
DEBRIEF_ENV = "AUTOFORM_DEBRIEF"
SKILLS = ("setup", "roadmap", "formalize")
RUNS, ISSUES = "runs", "issues"
ISSUE_REPOSITORY = "facebookresearch/autoform-bot"
MAX_ISSUE_URL = 8000

_RUN_ID = re.compile(r"\A[0-9a-f]{32}\Z")

QUESTION = """This {skill} run is over. Before you finish, tell AutoformBot's maintainers
what got in your way. This is about the tooling and the workflow, not the
mathematics: skip anything that was hard only because the proof was hard.

Answer with a single JSON object and no other text. Do not wrap it in a Markdown code fence.

{
  "local_setup_friction": [
    {"what": "what went wrong",
     "impact": "what it cost: retries, time, a step skipped or done by hand",
     "workaround": "what you did instead, or null"}
  ],
  "autoform_friction": [
    {"area": "setup | roadmap | formalize | review | cli | lean-tools | claims | docs | other",
     "what": "what went wrong; quote the command and the error",
     "impact": "what it cost",
     "suggestion": "what would have avoided it, or null"}
  ],
  "nothing_to_report": false
}

`local_setup_friction` is friction caused by this machine or project and
independent of AutoformBot: it would have happened without it. Examples: a
missing or misconfigured toolchain, sandbox, network or permission limits, an
out-of-date build cache, the project's own build problems. Friction caused by
local modifications or a fork of AutoformBot (commands, tools, agents or
instructions that upstream AutoformBot does not have) belongs here too.

`autoform_friction` is friction or issues with AutoformBot itself: a command
that failed, misbehaved, was slow or reported something misleading; skill
instructions that were unclear, wrong or contradictory; a step you had to work
around; a command you needed and did not have. Include AutoformBot friction your
subagents reported to you. When a local problem is one AutoformBot could
reasonably have detected or explained, report it here too.

`autoform_friction` becomes a draft issue on AutoformBot's public tracker, which
the user reviews before sending. Describe each entry without local paths,
usernames, or the project's private content.

Report only friction you hit in this run; do not speculate. If you have nothing
to report, return `"nothing_to_report": true` with both arrays empty. Do not
invent entries to fill the schema."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def enabled() -> bool:
    """On unless opted out, on a platform that can store the answer."""
    opted_out = (os.environ.get(DEBRIEF_ENV) or "").strip().lower() in {"0", "false", "no", "off"}
    return not opted_out and store.directory_binding.DIRECTORY_BINDING_SUPPORTED


def _check_skill(skill: str) -> str:
    if skill not in SKILLS:
        raise DebriefError(f"skill must be one of {list(SKILLS)}: {skill!r}")
    return skill


def form(skill: str) -> str:
    return QUESTION.replace("{skill}", _check_skill(skill).capitalize())


def autoform_version() -> str:
    """The installed version, plus the plugin checkout's commit when it is one."""
    try:
        version = importlib.metadata.version("autoform")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    commit = _git(Path(__file__).resolve().parent.parent, "rev-parse", "--short=12", "HEAD")
    return f"{version} ({commit})" if commit else version


# ---------------------------------------------------------------------------
# Typed records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocalFriction:
    what: str
    impact: str
    workaround: str


@dataclass(frozen=True, slots=True)
class AutoformFriction:
    area: str
    what: str
    impact: str
    suggestion: str


@dataclass(frozen=True, slots=True)
class OrchestratorAnswer:
    """The agent's answer, normalised to the form's schema and bounded."""

    local_setup_friction: tuple[LocalFriction, ...] = ()
    autoform_friction: tuple[AutoformFriction, ...] = ()
    nothing_to_report: bool = False

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> OrchestratorAnswer:
        """Normalise an answer, fresh or stored; unknown keys are dropped."""
        return cls(
            local_setup_friction=tuple(
                LocalFriction(
                    what=_text(item.get("what")),
                    impact=_text(item.get("impact")),
                    workaround=_text(item.get("workaround")),
                )
                for item in _items(data.get("local_setup_friction"))
            ),
            autoform_friction=tuple(
                AutoformFriction(
                    area=_text(item.get("area"), 64),
                    what=_text(item.get("what")),
                    impact=_text(item.get("impact")),
                    suggestion=_text(item.get("suggestion")),
                )
                for item in _items(data.get("autoform_friction"))
            ),
            nothing_to_report=data.get("nothing_to_report") is True,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "local_setup_friction": [_fields(item) for item in self.local_setup_friction],
            "autoform_friction": [_fields(item) for item in self.autoform_friction],
            "nothing_to_report": self.nothing_to_report,
        }


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One run's orchestrator debrief."""

    run_id: str
    skill: str
    autoform_version: str
    recorded_at: str
    answer: OrchestratorAnswer

    def __post_init__(self) -> None:
        if not _RUN_ID.match(self.run_id):
            raise RecordError(f"run_id must be 32 lowercase hex digits: {self.run_id!r}")
        if self.skill not in SKILLS:
            raise RecordError(f"unknown skill: {self.skill!r}")

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            **{name: getattr(self, name) for name in self.__slots__ if name != "answer"},
            "answer": self.answer.to_json(),
        }

    @classmethod
    def from_json(cls, data: Any) -> RunRecord:
        if not isinstance(data, dict):
            raise RecordError("record must be a JSON object")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise RecordError(f"unsupported schema_version: {data.get('schema_version')!r}")
        answer = data.get("answer")
        if not isinstance(answer, dict):
            raise RecordError("answer must be an object")
        fields = {}
        for name in cls.__slots__:
            if name == "answer":
                continue
            value = data.get(name)
            if not isinstance(value, str):
                raise RecordError(f"{name} must be a string")
            fields[name] = _text(value, 128)
        return cls(**fields, answer=OrchestratorAnswer.from_json(answer))


@dataclass(frozen=True, slots=True)
class DraftIssue:
    """A draft for AutoformBot's tracker; ``url`` opens it pre-filled, for the user to submit."""

    title: str
    body: str
    path: Path
    url: str
    url_has_body: bool


# ---------------------------------------------------------------------------
# Draft issue
# ---------------------------------------------------------------------------


def _redactions(supplied: str | Path, project: Path) -> list[tuple[str, str]]:
    """Local paths to replace, longest first so a project inside home becomes ``$PROJECT``.

    Each path is listed both as written and resolved: an agent may write either
    (``/var`` or ``/private/var`` on macOS).
    """
    given = Path(supplied).expanduser().absolute()
    given = given.parent if given != project and (given / "roadmap").is_dir() else given
    pairs = {str(given): "$PROJECT", str(project): "$PROJECT"}
    for home in {Path.home(), Path.home().resolve()}:
        if len(str(home)) > 1:
            pairs.setdefault(str(home), "~")
    return sorted(pairs.items(), key=lambda pair: len(pair[0]), reverse=True)


# Home directories the explicit redactions missed: another user's, or a resolved alias.
_HOME_PATH = re.compile(r"(?:/Users|/home|[A-Za-z]:\\Users)[/\\][^/\\\s]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _issue_text(value: str, redactions: list[tuple[str, str]], empty: str = "Not reported") -> str:
    """Agent text as one line of an issue, without local paths or control characters.

    GitHub sanitises HTML itself, and the user reads the draft before sending it.
    """
    text = " ".join(_CONTROL.sub(" ", value).split())
    for path, placeholder in redactions:
        text = text.replace(path, placeholder)
    return _HOME_PATH.sub("~", text) or empty


def _issue_title(entry: RunRecord, redactions: list[tuple[str, str]]) -> str:
    items = entry.answer.autoform_friction
    first = _issue_text(items[0].what, redactions, "friction")
    first = first if len(first) <= 80 else first[:79] + "…"
    more = f" (+{len(items) - 1} more)" if len(items) > 1 else ""
    return f"Agent feedback from a {entry.skill.capitalize()} run: {first}{more}"


def _issue_body(entry: RunRecord, redactions: list[tuple[str, str]]) -> str:
    lines = [
        f"An agent drafted this after an AutoformBot {entry.skill.capitalize()} run. "
        "The user reviewed it before submitting it.",
        "",
        "## Friction with AutoformBot",
        "",
    ]
    for index, item in enumerate(entry.answer.autoform_friction, 1):
        lines += [
            f"### {index}. {_issue_text(item.what, redactions, 'Friction')}",
            "",
            f"- **Area:** {_issue_text(item.area, redactions)}",
            f"- **Impact:** {_issue_text(item.impact, redactions)}",
            f"- **Suggestion:** {_issue_text(item.suggestion, redactions)}",
            "",
        ]
    lines += [
        "## Environment",
        "",
        f"- **AutoformBot:** {_issue_text(entry.autoform_version, redactions)}",
        f"- **Skill:** {entry.skill}",
        f"- **Platform:** {platform.system()} {platform.machine()}, Python {platform.python_version()}",
        "",
    ]
    return "\n".join(lines)


def _draft_text(title: str, body: str, url: str, url_has_body: bool) -> str:
    """The draft as stored: the issue, then a footer with the link that is not part of it.

    The link can hold the whole draft, thousands of characters, so it lives here
    rather than in the agent's report. It is built when the draft is written, so
    the footer says, before the link, where edits go.
    """
    if url_has_body:
        note = (
            "The link holds the draft as it was recorded, so make your edits in the form; if you edited this "
            "file, copy its title into the form's title and the text between the title and this footer over "
            f"its body. [Open the AutoformBot issue form pre-filled with the recorded draft]({url})"
        )
    else:
        note = (
            "The draft is too long for a link, so the link fills in only the title as it was recorded: paste "
            "the text between the title and this footer as the body, and copy the title into the form's title "
            f"if you edited it. [Open the AutoformBot issue form]({url})"
        )
    return f"# {title}\n\n{body}\n---\n\nNot part of the issue; nothing has been sent. {note}\n"


def issue_url(title: str, body: str) -> tuple[str, bool]:
    """A new-issue link pre-filled with the draft, or with the title alone when the body is too long."""
    base = f"https://github.com/{ISSUE_REPOSITORY}/issues/new?"
    url = base + urlencode({"title": title, "body": body}, quote_via=quote)
    if len(url) <= MAX_ISSUE_URL:
        return url, True
    return base + urlencode({"title": title}, quote_via=quote), False


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


def record(
    project_or_blueprint: str | Path, answer_text: str, *, skill: str
) -> tuple[RunRecord, Path, DraftIssue | None]:
    """Validate an answer and store it; draft an issue when it reports AutoformBot friction."""
    _check_skill(skill)
    if len(answer_text.encode("utf-8", "surrogatepass")) > MAX_ANSWER_BYTES:
        raise DebriefError(f"the answer exceeds {MAX_ANSWER_BYTES} bytes")
    answer = OrchestratorAnswer.from_json(parse_answer(answer_text))
    entry = RunRecord(
        run_id=uuid.uuid4().hex,
        skill=skill,
        autoform_version=autoform_version(),
        recorded_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        answer=answer,
    )
    data = _encode(entry.to_json())
    if len(data) > store.MAX_FILE_BYTES:
        raise DebriefError("the answer is too large to store; shorten it")
    project = project_dir(project_or_blueprint)
    root = debrief_root(project)
    name = entry.run_id
    issue = None
    with store.open_root(root) as root_fd:
        with store.open_subdir(root_fd, RUNS) as runs:
            store.create_exclusive(runs, f"{name}.json", data)
        if answer.autoform_friction:
            redactions = _redactions(project_or_blueprint, project)
            title, body = _issue_title(entry, redactions), _issue_body(entry, redactions)
            url, url_has_body = issue_url(title, body)
            with store.open_subdir(root_fd, ISSUES) as issues:
                draft = _draft_text(title, body, url, url_has_body)
                store.create_exclusive(issues, f"{name}.md", draft.encode("utf-8"))
            issue = DraftIssue(title, body, root / ISSUES / f"{name}.md", url, url_has_body)
    return entry, root / RUNS / f"{name}.json", issue


def load_runs(root: Path) -> list[RunRecord]:
    """Every valid run record, oldest first. Invalid or mismatched files are skipped."""
    runs: list[RunRecord] = []
    with store.open_root(root) as root_fd, store.open_subdir(root_fd, RUNS) as runs_fd:
        for name in store.list_names(runs_fd, ".json"):
            try:
                entry = RunRecord.from_json(_decode(store.read_bounded(runs_fd, name)))
            except (OSError, RecordError):
                continue
            if f"{entry.run_id}.json" == name:
                runs.append(entry)
    return sorted(runs, key=lambda entry: (entry.recorded_at, entry.run_id))


__all__ = [
    "AutoformFriction",
    "DraftIssue",
    "LocalFriction",
    "OrchestratorAnswer",
    "RunRecord",
    "enabled",
    "form",
    "issue_url",
    "load_runs",
    "record",
]
