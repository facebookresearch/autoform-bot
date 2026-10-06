"""Opt-in feedback form ("debrief") an agent fills in after a formalization attempt.

The agent that worked a leaf is the only witness to what the proof cost: the
finished Lean records what it wrote, not what it searched for and could not
find, nor what it tried and abandoned. Once the attempt is over (its result
integrated and its claim released, or the leaf abandoned), the same agent asks
``autoform debrief form`` for the form, answers it from its own session, and
stores the answer with ``autoform debrief record``. Recording cannot change the
attempt: the outcome is read from the roadmap runtime, not from the agent.

Configuration (environment):

``AUTOFORM_DEBRIEF``
    ``1`` / ``true`` / ``yes`` makes ``form`` print the form; otherwise it says
    debriefs are disabled and the agent skips the step.
``AUTOFORM_DEBRIEF_DIR``
    Absolute store directory outside the working tree. Defaults to
    ``autoform/debriefs`` inside the repository's Git common directory, which
    every worktree of a checkout shares and Git never tracks, else to
    ``$XDG_STATE_HOME/autoform/debriefs/<project>-<hash>``.
``AUTOFORM_DEBRIEF_QUESTION_FILE``
    A file whose text replaces :data:`DEFAULT_QUESTION`. ``{node_id}`` and
    ``{outcome}`` are substituted literally; nothing else is interpreted.

Store layout (see :mod:`autoform_cli.debrief_store` for the file discipline)::

    records/<attempt_id>.json   one DebriefRecord per attempt, schema_version 1
    views/                      Markdown rendered from validated records (derived)

:func:`load_records` is the typed read API for downstream consumers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import debrief_store as store
from .runtime import RuntimeNode, load_runtime_graph, resolve_runtime_paths

SCHEMA_VERSION = 1
DEBRIEF_ENV = "AUTOFORM_DEBRIEF"
DEBRIEF_DIR_ENV = "AUTOFORM_DEBRIEF_DIR"
DEBRIEF_QUESTION_FILE_ENV = "AUTOFORM_DEBRIEF_QUESTION_FILE"

RECORDS, VIEWS = "records", "views"
PHASES = frozenset({"statement", "proof"})
OUTCOMES = frozenset({"succeeded", "not-succeeded"})

MAX_ANSWER_BYTES = 256 * 1024
MAX_QUESTION_BYTES = 64 * 1024
MAX_ITEMS = 20
MAX_TEXT = 600
MAX_GOAL = 2000
MAX_NOTE = 2000
MAX_OTHER_FIELDS = 10
MAX_KEY = 64
GIT_TIMEOUT_SECONDS = 10.0

DEFAULT_QUESTION = """This attempt is finished and its outcome is already recorded in the roadmap —
**nothing you say now changes it**, and there is nothing left to fix. This is a
separate question about tooling, not about the proof.

You have just worked on `{node_id}`. Outcome: `{outcome}`.

We maintain project proof infrastructure — helper lemmas, simp and Aesop rules,
wrapper tactics, imports, and search/tooling support — and want to know what to
add or improve. You are the only witness to what this proof actually cost: the
finished file records what you wrote, not what you searched for and could not
find, nor what you tried and abandoned.

Answer with a single JSON object and no other text. Do not wrap it in a Markdown code fence.

{
  "friction": [
    {
      "file": "path/to/File.lean",
      "line": 111,
      "lines": 7,
      "what": "one sentence: what this block establishes",
      "why_hard": "lemma-search | lemma-name-guessing | repeated-boilerplate | side-goal-plumbing | rewrite-plumbing | other",
      "goal": "example ... := by sorry   (best effort, need not compile)"
    }
  ],
  "searched_for": [
    {"wanted": "what missing fact or infrastructure you needed",
     "searched": "the grep/rg terms or Mathlib names you tried",
     "why_hard": "missing-infrastructure | repeated-failed-search | misleading-name | inaccessible-import | complex-workaround | other",
     "found": false, "resolution": "what you eventually found or had to do instead"}
  ],
  "infrastructure_proposals": [
    {
      "kind": "tactic-idea | helper-lemma | simp-rule | aesop-rule | wrapper-tactic | import-export | search-tooling | other",
      "name": "snake_case_slug or null",
      "what": "one sentence: the reusable infrastructure or rough automation idea that might help",
      "evidence": "friction 1 | searched_for 2 | other"
    }
  ],
  "nothing_to_report": false
}

Rules for `friction`:
- Only blocks of **5 or more lines** where you knew what to do and it still
  took that long. Skip short steps where you named the right lemma first time.
- Skip blocks that were hard *mathematically*. Infrastructure can remove routine
  proof work; it cannot replace the argument.
- Friction is worth recording even when you do not know the right improvement.

Rules for `infrastructure_proposals`:
- A proposal may be any reusable infrastructure improvement. It need not
  correspond to a `friction` block. `evidence` is provenance, not a claim that
  the proposal is known to work; list every relevant entry rather than
  duplicating a proposal.
- Speculative tactic ideas are welcome: a suggestive name plus the proof pattern
  it might automate is enough.
- A `helper-lemma` should describe its approximate statement and where it would
  be reused. An `import-export` proposal should name the missing module.
- An empty `infrastructure_proposals` array is perfectly valid.

Rules for `searched_for`:
- This is a friction report, **not a history of searches**. Include an entry
  only when needed infrastructure was absent or inaccessible, several searches
  or name guesses failed, or the search ended in a materially more complex
  workaround. Omit lookups that succeeded on the first attempt.

If you have no useful notes, that is a complete and valid answer: return
`"nothing_to_report": true` with every array empty. Do not invent entries to
fill the schema."""

_JSON_FENCE = re.compile(r"\A```(?:json)?\s*(.*?)\s*```\s*\Z", re.IGNORECASE | re.DOTALL)
_ANSWER_KEYS = frozenset({"friction", "searched_for", "infrastructure_proposals", "nothing_to_report"})
_ATTEMPT_ID = re.compile(r"\A[0-9a-f]{32}\Z")


class DebriefError(ValueError):
    """The debrief cannot be produced or recorded as asked."""


class RecordError(ValueError):
    """A stored record is not a valid record of the current schema."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def enabled() -> bool:
    return (os.environ.get(DEBRIEF_ENV) or "").strip().lower() in {"1", "true", "yes"}


def _git_common_dir(project: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = Path(result.stdout.strip())
    return path.resolve() if result.returncode == 0 and path.is_absolute() and path.is_dir() else None


def debrief_root(project_or_blueprint: str | Path) -> Path:
    """Resolve the store directory, which must not lie in the working tree.

    Git's common directory is the exception: it is inside a main checkout but
    untracked, and shared by every worktree, so all agents of one repository
    record into the same store.
    """
    project = resolve_runtime_paths(project_or_blueprint).project_root.resolve()
    common = _git_common_dir(project)
    configured = (os.environ.get(DEBRIEF_DIR_ENV) or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise DebriefError(f"{DEBRIEF_DIR_ENV} must be an absolute path: {configured!r}")
    elif common is not None:
        path = common / "autoform" / "debriefs"
    else:
        state = Path(os.environ.get("XDG_STATE_HOME") or "")
        if not state.is_absolute():
            state = Path.home() / ".local" / "state"
        digest = hashlib.sha256(str(project).encode("utf-8", "surrogateescape")).hexdigest()[:12]
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", project.name).strip("-.") or "project"
        path = state / "autoform" / "debriefs" / f"{name}-{digest}"
    root = path.resolve()
    in_tree = root == project or project in root.parents
    in_git = common is not None and (root == common or common in root.parents)
    if in_tree and not in_git:
        raise DebriefError(f"debrief store {root} must be outside the working tree {project}")
    return root


def build_question(node_id: str, outcome: str) -> str:
    path = (os.environ.get(DEBRIEF_QUESTION_FILE_ENV) or "").strip()
    if not path:
        template = DEFAULT_QUESTION
    else:
        with Path(path).expanduser().open("rb") as handle:
            data = handle.read(MAX_QUESTION_BYTES + 1)
        if len(data) > MAX_QUESTION_BYTES:
            raise DebriefError(f"{DEBRIEF_QUESTION_FILE_ENV} exceeds {MAX_QUESTION_BYTES} bytes")
        template = data.decode("utf-8", "replace")
    return template.replace("{node_id}", node_id).replace("{outcome}", outcome)


# ---------------------------------------------------------------------------
# Normalisation: every stored string is bounded and encodable
# ---------------------------------------------------------------------------


def _clean(text: str, limit: int) -> str:
    text = text.encode("utf-8", "replace").decode("utf-8").replace("\x00", "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return _clean(str(value).strip(), limit)
    try:
        encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    except (TypeError, ValueError, RecursionError):
        encoded = type(value).__name__
    return _clean(encoded, limit)


def _count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, int) and 0 <= value <= 10**7:
        return value
    return None


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)][:MAX_ITEMS]


# ---------------------------------------------------------------------------
# Typed records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Friction:
    file: str
    line: int | None
    lines: int | None
    what: str
    why_hard: str
    goal: str


@dataclass(frozen=True, slots=True)
class SearchReport:
    wanted: str
    searched: str
    why_hard: str
    found: bool | None
    resolution: str


@dataclass(frozen=True, slots=True)
class Proposal:
    kind: str
    name: str
    what: str
    evidence: str


@dataclass(frozen=True, slots=True)
class DebriefAnswer:
    """The agent's answer, normalised to the form's schema and bounded.

    ``other_fields`` keeps keys a custom question asked for as compact JSON.
    """

    friction: tuple[Friction, ...] = ()
    searched_for: tuple[SearchReport, ...] = ()
    infrastructure_proposals: tuple[Proposal, ...] = ()
    nothing_to_report: bool = False
    other_fields: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_model(cls, data: dict[str, Any]) -> DebriefAnswer:
        """Normalise a parsed answer; unknown keys become ``other_fields``."""
        return cls._build(data, [(key, value) for key, value in data.items() if key not in _ANSWER_KEYS])

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DebriefAnswer:
        """Re-validate a stored answer."""
        other = data.get("other_fields")
        if not isinstance(other, list) or not all(
            isinstance(entry, list) and len(entry) == 2 and all(isinstance(part, str) for part in entry)
            for entry in other
        ):
            raise RecordError("other_fields must be a list of [key, value] string pairs")
        return cls._build(data, [(key, value) for key, value in other])

    @classmethod
    def _build(cls, data: dict[str, Any], extras: list[tuple[Any, Any]]) -> DebriefAnswer:
        return cls(
            friction=tuple(
                Friction(
                    file=_text(item.get("file"), 512),
                    line=_count(item.get("line")),
                    lines=_count(item.get("lines")),
                    what=_text(item.get("what")),
                    why_hard=_text(item.get("why_hard"), 64),
                    goal=_text(item.get("goal"), MAX_GOAL),
                )
                for item in _items(data.get("friction"))
            ),
            searched_for=tuple(
                SearchReport(
                    wanted=_text(item.get("wanted")),
                    searched=_text(item.get("searched")),
                    why_hard=_text(item.get("why_hard"), 64),
                    found=_flag(item.get("found")),
                    resolution=_text(item.get("resolution")),
                )
                for item in _items(data.get("searched_for"))
            ),
            infrastructure_proposals=tuple(
                Proposal(
                    kind=_text(item.get("kind"), 64),
                    name=_text(item.get("name"), 128),
                    what=_text(item.get("what")),
                    evidence=_text(item.get("evidence")),
                )
                for item in _items(data.get("infrastructure_proposals"))
            ),
            nothing_to_report=data.get("nothing_to_report") is True,
            other_fields=tuple(
                (_text(key, MAX_KEY), _text(value, MAX_GOAL)) for key, value in extras[:MAX_OTHER_FIELDS]
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "friction": [_fields(item) for item in self.friction],
            "searched_for": [_fields(item) for item in self.searched_for],
            "infrastructure_proposals": [_fields(item) for item in self.infrastructure_proposals],
            "nothing_to_report": self.nothing_to_report,
            "other_fields": [list(entry) for entry in self.other_fields],
        }


@dataclass(frozen=True, slots=True)
class DebriefRecord:
    """One finished attempt's debrief, with its outcome as the roadmap runtime reported it."""

    attempt_id: str
    node_id: str
    article_id: str
    article_revision: str
    source_revision: str
    phase: str
    outcome: str
    note: str
    worker_id: str
    recorded_at: str
    answer: DebriefAnswer

    def __post_init__(self) -> None:
        if not _ATTEMPT_ID.match(self.attempt_id):
            raise RecordError(f"attempt_id must be 32 lowercase hex digits: {self.attempt_id!r}")
        if self.phase not in PHASES:
            raise RecordError(f"unknown phase: {self.phase!r}")
        if self.outcome not in OUTCOMES:
            raise RecordError(f"unknown outcome: {self.outcome!r}")

    @property
    def outcome_label(self) -> str:
        return _outcome_label(self.phase, self.outcome, self.note)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            **{name: getattr(self, name) for name in self.__slots__ if name != "answer"},
            "answer": self.answer.to_json(),
        }

    @classmethod
    def from_json(cls, data: Any) -> DebriefRecord:
        if not isinstance(data, dict):
            raise RecordError("record must be a JSON object")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise RecordError(f"unsupported schema_version: {data.get('schema_version')!r}")
        answer = data.get("answer")
        if not isinstance(answer, dict):
            raise RecordError("answer must be an object")
        limits = {"note": MAX_NOTE, "node_id": 256, "worker_id": 128}
        fields = {}
        for name in cls.__slots__:
            if name == "answer":
                continue
            value = data.get(name)
            if not isinstance(value, str):
                raise RecordError(f"{name} must be a string")
            fields[name] = _text(value, limits.get(name, 128))
        return cls(**fields, answer=DebriefAnswer.from_json(answer))


def _outcome_label(phase: str, outcome: str, note: str) -> str:
    if outcome == "succeeded":
        return f"{phase} formalized"
    return f"{phase} not formalized: {note or 'no reason given'}"


def _fields(item: Any) -> dict[str, Any]:
    return {name: getattr(item, name) for name in item.__slots__}


def _encode(record: dict[str, Any]) -> bytes:
    return (json.dumps(record, ensure_ascii=True, sort_keys=True, indent=1) + "\n").encode("ascii")


def _decode(data: bytes) -> Any:
    try:
        return json.loads(data.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise RecordError(f"not an ASCII JSON record: {error}") from error


def parse_answer(text: str) -> dict[str, Any]:
    """Parse a JSON-object answer, accepting one optional Markdown fence."""
    raw = text.strip()
    fenced = _JSON_FENCE.fullmatch(raw)
    candidate = fenced.group(1).strip() if fenced is not None else raw
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, RecursionError) as error:
        raise DebriefError(f"the answer is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise DebriefError("the answer must be a single JSON object")
    return parsed


# ---------------------------------------------------------------------------
# Form and record
# ---------------------------------------------------------------------------


def _select(project_or_blueprint: str | Path, selector: str, lean_root: str | Path | None):
    runtime = load_runtime_graph(project_or_blueprint, lean_root=lean_root)
    matches = [
        node
        for node in runtime.nodes
        if node.id == selector or (node.article_id is not None and node.article_id == selector)
    ]
    if len(matches) != 1:
        raise DebriefError(f"{selector!r} must match exactly one article; it matches {len(matches)}")
    return runtime.source_revision, matches[0]


def _outcome(node: RuntimeNode, phase: str) -> str:
    if phase not in PHASES:
        raise DebriefError(f"phase must be one of {sorted(PHASES)}: {phase!r}")
    done = node.status.proved if phase == "proof" else node.status.stated
    return "succeeded" if done else "not-succeeded"


def form(
    project_or_blueprint: str | Path,
    selector: str,
    *,
    phase: str,
    note: str = "",
    lean_root: str | Path | None = None,
) -> str:
    """The form for one finished attempt, with the runtime's outcome filled in."""
    _, node = _select(project_or_blueprint, selector, lean_root)
    return build_question(node.id, _outcome_label(phase, _outcome(node, phase), _text(note, MAX_NOTE)))


def record(
    project_or_blueprint: str | Path,
    selector: str,
    answer_text: str,
    *,
    phase: str,
    note: str = "",
    worker_id: str = "",
    lean_root: str | Path | None = None,
) -> tuple[DebriefRecord, Path]:
    """Validate an answer and store it as a new record; return it and its path."""
    if len(answer_text.encode("utf-8", "surrogatepass")) > MAX_ANSWER_BYTES:
        raise DebriefError(f"the answer exceeds {MAX_ANSWER_BYTES} bytes")
    answer = DebriefAnswer.from_model(parse_answer(answer_text))
    source_revision, node = _select(project_or_blueprint, selector, lean_root)
    outcome = _outcome(node, phase)
    entry = DebriefRecord(
        attempt_id=uuid.uuid4().hex,
        node_id=_text(node.id, 256),
        article_id=node.article_id or "",
        article_revision=node.source_sha256 or "",
        source_revision=source_revision,
        phase=phase,
        outcome=outcome,
        note=_text(note, MAX_NOTE),
        worker_id=_text(worker_id, 128),
        recorded_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        answer=answer,
    )
    root = debrief_root(project_or_blueprint)
    name = f"{entry.attempt_id}.json"
    with store.open_root(root) as root_fd, store.open_subdir(root_fd, RECORDS) as records:
        store.create_exclusive(records, name, _encode(entry.to_json()))
    return entry, root / RECORDS / name


def load_records(root: Path) -> list[DebriefRecord]:
    """Every valid record, oldest first. Invalid or mismatched files are skipped."""
    records: list[DebriefRecord] = []
    with store.open_root(root) as root_fd, store.open_subdir(root_fd, RECORDS) as records_fd:
        for name in store.list_names(records_fd, ".json"):
            try:
                entry = DebriefRecord.from_json(_decode(store.read_bounded(records_fd, name)))
            except (OSError, RecordError):
                continue
            if f"{entry.attempt_id}.json" == name:
                records.append(entry)
    return sorted(records, key=lambda entry: (entry.recorded_at, entry.attempt_id))


# ---------------------------------------------------------------------------
# Derived Markdown views
# ---------------------------------------------------------------------------

_MARKDOWN_PUNCTUATION = re.compile(r"([!-/:-@\[-`{-~])")


def _md(value: Any, empty: str = "Not reported") -> str:
    """Agent text as one inert Markdown line: every ASCII punctuation mark is escaped."""
    text = " ".join(str(value).split()) if value is not None and value != "" else empty
    return _MARKDOWN_PUNCTUATION.sub(r"\\\1", text)


def _code_block(text: str, language: str = "") -> list[str]:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{language}", text, fence]


def render_record_markdown(entry: DebriefRecord) -> str:
    answer = entry.answer
    lines = [
        f"# Proof debrief: {_md(entry.node_id)}",
        "",
        f"- **Outcome:** {_md(entry.outcome_label)}",
        f"- **Article revision:** {_md(entry.article_revision)}",
        f"- **Recorded at:** {_md(entry.recorded_at)}",
        f"- **Worker:** {_md(entry.worker_id)}",
        "",
        "## At a glance",
        "",
        f"- Friction blocks: {len(answer.friction)}",
        f"- Search reports: {len(answer.searched_for)}",
        f"- Infrastructure proposals: {len(answer.infrastructure_proposals)}",
        f"- Nothing to report: {'yes' if answer.nothing_to_report else 'no'}",
        "",
        "## Infrastructure proposals",
        "",
    ]
    if not answer.infrastructure_proposals:
        lines += ["No infrastructure proposals were reported.", ""]
    for index, proposal in enumerate(answer.infrastructure_proposals, 1):
        lines += [
            f"### {index}. {_md(proposal.name or proposal.kind, 'Infrastructure improvement')}",
            "",
            f"- **Kind:** {_md(proposal.kind)}",
            f"- **Improvement:** {_md(proposal.what)}",
            f"- **Evidence:** {_md(proposal.evidence)}",
            "",
        ]
    lines += ["## Mechanical friction", ""]
    if not answer.friction:
        lines += ["No qualifying mechanical blocks were reported.", ""]
    for index, item in enumerate(answer.friction, 1):
        location = f"{item.file or '?'}:{item.line if item.line is not None else '?'}"
        lines += [
            f"### {index}. {_md(item.what, 'Reported block')}",
            "",
            f"- **Location:** {_md(location)}",
            f"- **Block size:** {item.lines if item.lines is not None else '?'} lines",
            f"- **Difficulty:** {_md(item.why_hard)}",
            "",
        ]
        if item.goal:
            lines += ["**Goal sketch:**", "", *_code_block(item.goal, "lean"), ""]
    lines += ["## Searches and missing infrastructure", ""]
    if not answer.searched_for:
        lines += ["No searches were reported.", ""]
    for index, item in enumerate(answer.searched_for, 1):
        found = "unknown" if item.found is None else ("yes" if item.found else "no")
        lines += [
            f"### {index}. {_md(item.wanted, 'Search')}",
            "",
            f"- **Found:** {found}",
            f"- **Difficulty:** {_md(item.why_hard)}",
            f"- **Searched:** {_md(item.searched)}",
            f"- **Resolution:** {_md(item.resolution)}",
            "",
        ]
    if answer.other_fields:
        lines += ["## Other fields", ""]
        for key, value in answer.other_fields:
            lines += [f"### {_md(key)}", "", *_code_block(value), ""]
    return "\n".join(lines)


def render(root: Path, out_dir: Path | None = None) -> list[Path]:
    """Rebuild Markdown views from validated records; return the report paths."""
    out = (out_dir or root / VIEWS).expanduser().resolve()
    entries = load_records(root)
    written: list[Path] = []
    index = ["# Proof debriefs", "", "Rendered from validated debrief records. Oldest first.", ""]
    with store.open_root(out) as out_fd, store.open_subdir(out_fd, "reports") as reports:
        for entry in entries:
            name = f"{entry.attempt_id}.md"
            store.replace_atomic(reports, name, render_record_markdown(entry).encode("utf-8"))
            written.append(out / "reports" / name)
            index.append(f"- [{_md(entry.node_id)} — {_md(entry.outcome_label)}](reports/{name})")
        if not entries:
            index.append("No debrief records found.")
        store.replace_atomic(out_fd, "README.md", ("\n".join(index) + "\n").encode("utf-8"))
    return written


__all__ = [
    "DebriefAnswer",
    "DebriefError",
    "DebriefRecord",
    "Friction",
    "Proposal",
    "RecordError",
    "SearchReport",
    "build_question",
    "debrief_root",
    "enabled",
    "form",
    "load_records",
    "record",
    "render",
]
